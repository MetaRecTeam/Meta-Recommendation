"""B4 Postgres contract: the durable store behaves like the in-memory one.

Skips without WP8_TRACE_TEST_DATABASE_URL (or DATABASE_URL). The schema comes
from the 20261115_0008 migration, which must already be applied.

Everything asserted here is also asserted for InMemoryTraceStore in
test_wp8_trace_store.py. Two stores that drift apart in a corner nobody tests
is the failure this file exists to prevent.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import pytest

from langgraph_metarec.wp8_trace.config import validate_trace_config, default_trace_config
from langgraph_metarec.wp8_trace.interfaces import Projection
from langgraph_metarec.wp8_trace.models import GraphTraceV1
from langgraph_metarec.wp8_trace.redaction import safe_error
from langgraph_metarec.wp8_trace.storage import TraceNotFound
from tests.wp8_store_support import (
    T0,
    definition,
    draft,
    fat_projection,
    graph_start,
    instance,
)

pytestmark = pytest.mark.runtime_contract


def _database_url() -> str | None:
    """A database to test against, or None to skip.

    WP8_TRACE_TEST_DATABASE_URL wins, so a scratch database can be used
    without touching the one in .env. Read at call time rather than import
    time, because conftest loads .env after this module is imported.
    """
    return os.getenv("WP8_TRACE_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")


async def _schema_present(url: str) -> bool:
    """Whether migration 20261115_0008 has been applied to this database."""
    from sqlalchemy import text as sql
    from sqlalchemy.ext.asyncio import create_async_engine

    from business_db import normalize_async_database_url

    engine = create_async_engine(normalize_async_database_url(url))
    try:
        async with engine.connect() as connection:
            return bool(
                (await connection.execute(sql("SELECT to_regclass('public.wp8_traces')"))).scalar()
            )
    except Exception:
        return False
    finally:
        await engine.dispose()


def _limits(**overrides):
    payload = default_trace_config().model_dump(mode="json")
    payload["limits"].update(overrides)
    return validate_trace_config(payload)


class _Fixture:
    """One store, one scratch trace id, torn down afterwards."""

    def __init__(self, store, trace_id, run_id):
        self.store = store
        self.trace_id = trace_id
        self.run_id = run_id


async def _open(config=None, clock=None):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from langgraph_metarec.wp8_trace.postgres_storage import PostgresTraceStore
    from business_db import normalize_async_database_url

    engine = create_async_engine(normalize_async_database_url(_database_url()))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    store = PostgresTraceStore(maker, clock=clock or (lambda: T0))
    suffix = uuid.uuid4().hex[:12]
    trace_id, run_id = f"pytest-tr-{suffix}", f"pytest-run-{suffix}"
    await store.start_trace(trace_id, run_id, config=config, definitions=[definition()])
    return _Fixture(store, trace_id, run_id), engine


def _d(fixture, **kwargs):
    kwargs.setdefault("trace_id", fixture.trace_id)
    kwargs.setdefault("debug_run_id", fixture.run_id)
    return draft(**kwargs)


async def _require_database() -> str:
    """The database to use, or skip with a reason the reader can act on."""
    url = _database_url()
    if not url:
        pytest.skip("set DATABASE_URL or WP8_TRACE_TEST_DATABASE_URL to run the "
                    "WP8 Postgres store contract tests")
    if not await _schema_present(url):
        pytest.skip("wp8_traces is missing; run `alembic upgrade head` on this database "
                    "(migration 20261115_0008) to run the WP8 Postgres store tests")
    return url


def pg_test(fn):
    """Run one coroutine against Postgres, cleaning up whatever it created."""

    @pytest.mark.asyncio
    async def wrapper():
        await _require_database()
        fixture, engine = await _open()
        try:
            await fn(fixture)
        finally:
            await fixture.store.delete(fixture.trace_id)
            await engine.dispose()

    wrapper.__name__ = fn.__name__
    return wrapper


def pg_test_with(config_factory):
    """Same, for a trace that needs non-default limits."""

    def decorate(fn):
        @pytest.mark.asyncio
        async def wrapper():
            await _require_database()
            fixture, engine = await _open(config=config_factory())
            try:
                await fn(fixture)
            finally:
                await fixture.store.delete(fixture.trace_id)
                await engine.dispose()

        wrapper.__name__ = fn.__name__
        return wrapper

    return decorate


# --- sequencing (R7) ---------------------------------------------------------

@pg_test
async def test_sequence_starts_at_one_and_is_contiguous(f):
    for expected in (1, 2, 3):
        assert (await f.store.append(f.trace_id, _d(f))).sequence == expected


@pg_test
async def test_concurrent_appends_produce_unique_contiguous_sequences(f):
    # The reason sequence allocation holds FOR UPDATE on the trace row. Without
    # it, these coroutines read the same next_sequence and collide.
    results = await asyncio.gather(*[f.store.append(f.trace_id, _d(f)) for _ in range(60)])
    sequences = sorted(r.sequence for r in results)
    assert sequences == list(range(1, 61))
    page = await f.store.read_page(f.trace_id, limit=500)
    assert [e.sequence for e in page.events] == list(range(1, 61))


@pg_test
async def test_an_append_to_an_unopened_trace_is_dropped(f):
    result = await f.store.append("pytest-never-opened", _d(f))
    assert result.status == "dropped" and result.sequence is None


@pg_test
async def test_opening_the_same_trace_twice_keeps_what_was_captured(f):
    await f.store.append(f.trace_id, _d(f))
    await f.store.start_trace(f.trace_id, f.run_id, definitions=[definition()])
    assert (await f.store.read_page(f.trace_id)).event_window.returned == 1


# --- caps and truncation -----------------------------------------------------

@pg_test_with(lambda: _limits(max_event_bytes=1000))
async def test_an_oversized_event_is_dropped_and_counted(f):
    result = await f.store.append(f.trace_id, _d(f, projection=fat_projection()))
    assert result.status == "dropped"
    page = await f.store.read_page(f.trace_id)
    assert page.event_window.returned == 0 and page.dropped_event_count == 1


@pg_test_with(lambda: _limits(max_event_bytes=1000))
async def test_an_oversized_event_does_not_truncate_the_whole_trace(f):
    await f.store.append(f.trace_id, _d(f, projection=fat_projection()))
    assert (await f.store.append(f.trace_id, _d(f))).status == "accepted"
    assert (await f.store.read_page(f.trace_id)).truncated is False


@pg_test_with(lambda: _limits(max_events=3))
async def test_reaching_the_event_cap_writes_one_marker_and_stops(f):
    for _ in range(10):
        await f.store.append(f.trace_id, _d(f))
    page = await f.store.read_page(f.trace_id)
    kinds = [e.event_type for e in page.events]
    assert page.truncated is True
    assert kinds.count("trace_truncated") == 1 and kinds[-1] == "trace_truncated"
    assert page.dropped_event_count == 7


@pg_test_with(lambda: _limits(max_events=1))
async def test_the_truncation_marker_is_not_a_lifecycle_span(f):
    await f.store.append(f.trace_id, _d(f))
    await f.store.append(f.trace_id, _d(f))
    page = await f.store.read_page(f.trace_id)
    marker = [e for e in page.events if e.event_type == "trace_truncated"][0]
    assert marker.node_id is None and marker.run_id is None and marker.parent_run_id is None
    assert marker.runnable_type == "trace_system"


@pg_test_with(lambda: _limits(max_trace_bytes=2000))
async def test_the_total_byte_cap_also_truncates(f):
    for _ in range(50):
        await f.store.append(f.trace_id, _d(f))
    page = await f.store.read_page(f.trace_id)
    assert page.truncated is True and page.dropped_event_count >= 1


@pg_test_with(lambda: _limits(max_events=1))
async def test_truncation_does_not_change_the_trace_status(f):
    await f.store.append(f.trace_id, _d(f))
    await f.store.append(f.trace_id, _d(f))
    await f.store.finalize(f.trace_id, "completed")
    page = await f.store.read_page(f.trace_id)
    assert page.truncated is True and page.status == "completed"


# --- instances ---------------------------------------------------------------

@pg_test
async def test_an_open_instance_appears_in_the_topology(f):
    await f.store.open_instance(f.trace_id, instance())
    page = await f.store.read_page(f.trace_id)
    assert [i.graph_instance_id for i in page.topology.instances] == ["gi-1"]
    assert page.topology.instances[0].ended_sequence is None


@pg_test
async def test_closing_an_instance_records_the_ending_sequence(f):
    await f.store.open_instance(f.trace_id, instance())
    await f.store.close_instance(f.trace_id, "gi-1", 9)
    page = await f.store.read_page(f.trace_id)
    assert page.topology.instances[0].ended_sequence == 9


@pg_test
async def test_closing_an_unknown_instance_is_ignored(f):
    await f.store.close_instance(f.trace_id, "never-opened", 3)
    assert (await f.store.read_page(f.trace_id)).topology.instances == []


# --- status ------------------------------------------------------------------

@pg_test
async def test_a_new_trace_is_running(f):
    assert (await f.store.read_page(f.trace_id)).status == "running"


@pg_test
async def test_a_finalized_trace_cannot_be_reopened(f):
    await f.store.finalize(f.trace_id, "failed")
    await f.store.finalize(f.trace_id, "completed")
    assert (await f.store.read_page(f.trace_id)).status == "failed"


@pg_test
async def test_recovery_marks_a_crashed_trace_incomplete(f):
    assert await f.store.recover_running() >= 1
    assert (await f.store.read_page(f.trace_id)).status == "incomplete"


# --- paging ------------------------------------------------------------------

@pg_test
async def test_the_cursor_walks_the_whole_trace_without_gaps_or_repeats(f):
    for _ in range(25):
        await f.store.append(f.trace_id, _d(f))
    seen, cursor = [], 0
    while True:
        page = await f.store.read_page(f.trace_id, after_sequence=cursor, limit=7)
        seen.extend(e.sequence for e in page.events)
        cursor = page.event_window.next_after_sequence
        if not page.event_window.has_more:
            break
    assert seen == list(range(1, 26))


@pg_test
async def test_has_more_is_false_on_the_last_page(f):
    for _ in range(3):
        await f.store.append(f.trace_id, _d(f))
    assert (await f.store.read_page(f.trace_id, limit=10)).event_window.has_more is False


@pg_test
async def test_a_cursor_past_the_end_returns_an_empty_page(f):
    await f.store.append(f.trace_id, _d(f))
    page = await f.store.read_page(f.trace_id, after_sequence=99)
    assert page.event_window.returned == 0
    assert page.event_window.has_more is False
    assert page.event_window.next_after_sequence == 99


@pg_test
async def test_a_zero_limit_is_refused(f):
    with pytest.raises(ValueError):
        await f.store.read_page(f.trace_id, limit=0)


@pg_test
async def test_every_page_validates_against_the_contract(f):
    await f.store.append(f.trace_id, graph_start(trace_id=f.trace_id, debug_run_id=f.run_id))
    for _ in range(3):
        await f.store.append(f.trace_id, _d(f))
    page = await f.store.read_page(f.trace_id, limit=2)
    GraphTraceV1.model_validate(page.model_dump(mode="json"))


# --- summary -----------------------------------------------------------------

@pg_test
async def test_summary_counts_distinct_visited_nodes(f):
    await f.store.append(f.trace_id, _d(f, node_id="candidate_gather", run_id="r-1"))
    await f.store.append(f.trace_id, _d(f, node_id="candidate_gather", run_id="r-1"))
    await f.store.append(f.trace_id, _d(f, node_id="__start__", run_id="r-2"))
    assert (await f.store.read_page(f.trace_id)).summary.visited_nodes == 2


@pg_test
async def test_summary_counts_errors(f):
    await f.store.append(f.trace_id, _d(
        f, event_type="error", status="error",
        projection=Projection(error=safe_error(RuntimeError("x")))))
    assert (await f.store.read_page(f.trace_id)).summary.error_count == 1


@pg_test
async def test_redaction_summary_accumulates_across_events(f):
    await f.store.append(f.trace_id, _d(f, projection=Projection(
        redacted_paths=("a", "b"), truncated_paths=("c",))))
    await f.store.append(f.trace_id, _d(f, projection=Projection(redacted_paths=("d",))))
    summary = (await f.store.read_page(f.trace_id)).redaction_summary
    assert summary.redacted_value_count == 3 and summary.truncated_value_count == 1


# --- durability and retention ------------------------------------------------

@pg_test
async def test_a_trace_survives_a_new_store_object(f):
    # The point of this store: the page is still there when the process that
    # wrote it is gone.
    await f.store.append(f.trace_id, _d(f))
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from business_db import normalize_async_database_url
    from langgraph_metarec.wp8_trace.postgres_storage import PostgresTraceStore

    engine = create_async_engine(normalize_async_database_url(_database_url()))
    try:
        fresh = PostgresTraceStore(async_sessionmaker(engine, expire_on_commit=False))
        assert (await fresh.read_page(f.trace_id)).event_window.returned == 1
    finally:
        await engine.dispose()


@pg_test
async def test_expiry_follows_the_requested_retention(f):
    page = await f.store.read_page(f.trace_id)
    assert (page.expires_at - page.created_at).days == 7


@pg_test
async def test_deleting_a_trace_takes_its_events_with_it(f):
    await f.store.append(f.trace_id, _d(f))
    await f.store.open_instance(f.trace_id, instance())
    assert await f.store.delete(f.trace_id) is True
    assert await f.store.exists(f.trace_id) is False
    with pytest.raises(TraceNotFound):
        await f.store.read_page(f.trace_id)


@pg_test
async def test_an_expired_trace_is_purged(f):
    assert await f.store.purge_expired(T0 + timedelta(days=6)) == 0
    assert await f.store.purge_expired(T0 + timedelta(days=8)) >= 1
    assert await f.store.exists(f.trace_id) is False


@pg_test
async def test_a_trace_can_be_found_by_its_debug_run(f):
    assert await f.store.trace_id_for_run(f.run_id) == f.trace_id
    assert await f.store.trace_id_for_run("pytest-no-such-run") is None
