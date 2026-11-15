"""B3: sequencing, caps, truncation, instances and paged reads.

These tests describe the storage contract, not one implementation. B4's
Postgres store is held to the same file.
"""
import threading
from datetime import timedelta

import pytest

from langgraph_metarec.wp8_trace.config import default_trace_config
from langgraph_metarec.wp8_trace.models import DEFAULT_PAGE_EVENTS, MAX_PAGE_EVENTS, GraphTraceV1
from langgraph_metarec.wp8_trace.storage import InMemoryTraceStore, TraceNotFound
from tests.wp8_store_support import (
    T0,
    definition,
    draft,
    fat_projection,
    graph_start,
    instance,
    node_end,
)

pytestmark = pytest.mark.backend_unit


@pytest.fixture
def store():
    store = InMemoryTraceStore(clock=lambda: T0)
    store.start_trace("tr-1", "run-1", definitions=[definition()])
    return store


def limited(**overrides):
    """A store whose trace has deliberately small limits."""
    payload = default_trace_config().model_dump(mode="json")
    payload["limits"].update(overrides)
    from langgraph_metarec.wp8_trace.config import validate_trace_config
    store = InMemoryTraceStore(clock=lambda: T0)
    store.start_trace("tr-1", "run-1", config=validate_trace_config(payload),
                      definitions=[definition()])
    return store


# --- sequencing (R7) ---------------------------------------------------------

def test_sequence_starts_at_one_and_is_contiguous(store):
    for expected in (1, 2, 3):
        assert store.append("tr-1", draft()).sequence == expected


def test_event_id_is_assigned_by_the_store(store):
    store.append("tr-1", graph_start())
    page = store.read_page("tr-1")
    assert page.events[0].event_id
    assert page.events[0].sequence == 1


def test_concurrent_appends_produce_unique_contiguous_sequences(store):
    # R7 exists because sequence allocation and append must share one critical
    # section. If they did not, two threads would take the same number.
    total = 400
    errors = []

    def worker():
        try:
            for _ in range(total // 8):
                store.append("tr-1", draft())
        except Exception as exc:  # pragma: no cover - surfaced by the assert
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    page = store.read_page("tr-1", limit=MAX_PAGE_EVENTS)
    sequences = [e.sequence for e in page.events]
    assert len(set(sequences)) == len(sequences)
    assert sequences == list(range(1, len(sequences) + 1))


def test_an_append_to_an_unopened_trace_is_dropped():
    store = InMemoryTraceStore(clock=lambda: T0)
    result = store.append("never-opened", draft())
    assert result.status == "dropped"
    assert result.sequence is None


def test_starting_the_same_trace_twice_keeps_what_was_captured(store):
    store.append("tr-1", draft())
    store.start_trace("tr-1", "run-1", definitions=[definition()])
    assert store.read_page("tr-1").event_window.returned == 1


# --- caps and truncation -----------------------------------------------------

def test_an_oversized_event_is_dropped_and_counted():
    # A minimal event is a few hundred bytes; 1 KB admits one and refuses a fat one.
    store = limited(max_event_bytes=1000)
    result = store.append("tr-1", draft(projection=fat_projection()))
    assert result.status == "dropped"
    page = store.read_page("tr-1")
    assert page.event_window.returned == 0
    assert page.dropped_event_count == 1


def test_an_oversized_event_does_not_truncate_the_whole_trace():
    # One event being too big says nothing about the next one.
    store = limited(max_event_bytes=1000)
    store.append("tr-1", draft(projection=fat_projection()))
    assert store.append("tr-1", draft()).status == "accepted"
    assert store.read_page("tr-1").truncated is False


def test_reaching_the_event_cap_writes_one_marker_and_stops():
    store = limited(max_events=3)
    for _ in range(10):
        store.append("tr-1", draft())
    page = store.read_page("tr-1")
    assert page.truncated is True
    kinds = [e.event_type for e in page.events]
    assert kinds.count("trace_truncated") == 1
    assert kinds[-1] == "trace_truncated"
    assert page.dropped_event_count == 7


def test_the_truncation_marker_is_not_a_lifecycle_span():
    # The event matrix forbids node_id, run_id and parent_run_id on it.
    store = limited(max_events=1)
    store.append("tr-1", draft())
    store.append("tr-1", draft())
    marker = [e for e in store.read_page("tr-1").events if e.event_type == "trace_truncated"][0]
    assert marker.node_id is None
    assert marker.run_id is None
    assert marker.parent_run_id is None
    assert marker.runnable_type == "trace_system"


def test_everything_after_truncation_is_counted_not_stored():
    store = limited(max_events=2)
    for _ in range(6):
        store.append("tr-1", draft())
    page = store.read_page("tr-1")
    assert page.dropped_event_count == 4
    assert all(e.event_type != "node_start" or e.sequence <= 2 for e in page.events)


def test_the_total_byte_cap_also_truncates():
    store = limited(max_trace_bytes=2000)
    for _ in range(50):
        store.append("tr-1", draft())
    page = store.read_page("tr-1")
    assert page.truncated is True
    assert page.dropped_event_count >= 1


def test_truncation_does_not_change_the_trace_status():
    # DECISIONS.md R8: truncation sets truncated, it does not make a run fail.
    store = limited(max_events=1)
    store.append("tr-1", draft())
    store.append("tr-1", draft())
    store.finalize("tr-1", "completed")
    page = store.read_page("tr-1")
    assert page.truncated is True
    assert page.status == "completed"


# --- instances ---------------------------------------------------------------

def test_an_open_instance_appears_in_the_topology(store):
    store.open_instance("tr-1", instance())
    page = store.read_page("tr-1")
    assert [i.graph_instance_id for i in page.topology.instances] == ["gi-1"]
    assert page.topology.instances[0].ended_sequence is None


def test_closing_an_instance_records_the_ending_sequence(store):
    store.open_instance("tr-1", instance())
    store.close_instance("tr-1", "gi-1", 9)
    assert store.read_page("tr-1").topology.instances[0].ended_sequence == 9


def test_closing_an_unknown_instance_is_ignored(store):
    store.close_instance("tr-1", "never-opened", 3)  # must not raise
    assert store.read_page("tr-1").topology.instances == []


def test_definitions_are_carried_on_every_page(store):
    page = store.read_page("tr-1")
    assert [d.graph_definition_id for d in page.topology.definitions] == ["generic-domain@v1"]


# --- status ------------------------------------------------------------------

def test_a_new_trace_is_running(store):
    assert store.read_page("tr-1").status == "running"


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "incomplete"])
def test_finalize_sets_the_terminal_status(store, status):
    store.finalize("tr-1", status)
    assert store.read_page("tr-1").status == status


def test_a_finalized_trace_cannot_be_reopened(store):
    store.finalize("tr-1", "failed")
    store.finalize("tr-1", "completed")
    assert store.read_page("tr-1").status == "failed"


def test_recovery_marks_a_crashed_trace_incomplete(store):
    # The one status the store assigns itself: the process that would have
    # finalised it is gone (R8).
    assert store.recover_running() == 1
    assert store.read_page("tr-1").status == "incomplete"


def test_recovery_leaves_finished_traces_alone(store):
    store.finalize("tr-1", "completed")
    assert store.recover_running() == 0
    assert store.read_page("tr-1").status == "completed"


# --- paging ------------------------------------------------------------------

def test_a_page_reports_how_many_it_carries(store):
    for _ in range(5):
        store.append("tr-1", draft())
    page = store.read_page("tr-1", limit=2)
    assert page.event_window.returned == 2 == len(page.events)


def test_the_cursor_walks_the_whole_trace_without_gaps_or_repeats(store):
    for _ in range(25):
        store.append("tr-1", draft())
    seen, cursor = [], 0
    while True:
        page = store.read_page("tr-1", after_sequence=cursor, limit=7)
        seen.extend(e.sequence for e in page.events)
        cursor = page.event_window.next_after_sequence
        if not page.event_window.has_more:
            break
    assert seen == list(range(1, 26))


def test_has_more_is_false_on_the_last_page(store):
    for _ in range(3):
        store.append("tr-1", draft())
    assert store.read_page("tr-1", limit=10).event_window.has_more is False


def test_a_cursor_past_the_end_returns_an_empty_page(store):
    store.append("tr-1", draft())
    page = store.read_page("tr-1", after_sequence=99)
    assert page.event_window.returned == 0
    assert page.event_window.has_more is False
    assert page.event_window.next_after_sequence == 99


def test_the_page_limit_is_capped_at_the_contract_maximum(store):
    for _ in range(5):
        store.append("tr-1", draft())
    page = store.read_page("tr-1", limit=10_000)
    assert page.event_window.returned == 5


def test_a_zero_or_negative_limit_is_refused(store):
    with pytest.raises(ValueError):
        store.read_page("tr-1", limit=0)


def test_a_page_of_large_events_is_cut_by_bytes_not_count():
    store = limited(max_event_bytes=16_384, max_trace_bytes=2_097_152)
    for _ in range(40):
        store.append("tr-1", draft(projection=fat_projection()))
    page = store.read_page("tr-1", limit=DEFAULT_PAGE_EVENTS)
    assert page.event_window.returned < 40
    assert page.event_window.has_more is True


def test_every_page_validates_against_the_contract(store):
    for _ in range(4):
        store.append("tr-1", draft())
    page = store.read_page("tr-1", limit=2)
    GraphTraceV1.model_validate(page.model_dump(mode="json"))


# --- summary -----------------------------------------------------------------

def test_summary_counts_distinct_visited_nodes(store):
    store.append("tr-1", draft(node_id="candidate_gather", run_id="r-1"))
    store.append("tr-1", draft(node_id="candidate_gather", run_id="r-1"))
    store.append("tr-1", draft(node_id="__start__", run_id="r-2"))
    assert store.read_page("tr-1").summary.visited_nodes == 2


def test_summary_counts_errors(store):
    from langgraph_metarec.wp8_trace.redaction import safe_error
    from langgraph_metarec.wp8_trace.interfaces import Projection
    store.append("tr-1", draft(event_type="error", status="error",
                               projection=Projection(error=safe_error(RuntimeError("x")))))
    assert store.read_page("tr-1").summary.error_count == 1


def test_summary_has_no_completion_while_running(store):
    store.append("tr-1", draft())
    summary = store.read_page("tr-1").summary
    assert summary.completed_at is None and summary.duration_ms is None


def test_summary_reports_a_duration_once_finished():
    moments = iter([T0, T0 + timedelta(seconds=3)])
    store = InMemoryTraceStore(clock=lambda: next(moments))
    store.start_trace("tr-1", "run-1", definitions=[definition()])
    store.append("tr-1", graph_start())
    store.finalize("tr-1", "completed")
    assert store.read_page("tr-1").summary.duration_ms == pytest.approx(3000.0)


def test_redaction_summary_accumulates_across_events(store):
    from langgraph_metarec.wp8_trace.interfaces import Projection
    store.append("tr-1", draft(projection=Projection(redacted_paths=("a", "b"),
                                                     truncated_paths=("c",))))
    store.append("tr-1", draft(projection=Projection(redacted_paths=("d",))))
    summary = store.read_page("tr-1").redaction_summary
    assert summary.redacted_value_count == 3
    assert summary.truncated_value_count == 1


# --- retention and administration --------------------------------------------

def test_expiry_follows_the_requested_retention(store):
    page = store.read_page("tr-1")
    assert (page.expires_at - page.created_at).days == 7


def test_an_expired_trace_is_purged():
    store = InMemoryTraceStore(clock=lambda: T0)
    store.start_trace("tr-1", "run-1", definitions=[definition()])
    assert store.purge_expired(T0 + timedelta(days=6)) == 0
    assert store.purge_expired(T0 + timedelta(days=8)) == 1
    assert not store.exists("tr-1")


def test_reading_a_missing_trace_raises(store):
    with pytest.raises(TraceNotFound):
        store.read_page("no-such-trace")


def test_delete_removes_a_trace(store):
    assert store.delete("tr-1") is True
    assert store.delete("tr-1") is False


def test_a_trace_can_be_found_by_its_debug_run(store):
    assert store.trace_id_for_run("run-1") == "tr-1"
    assert store.trace_id_for_run("other") is None


def test_the_store_satisfies_the_agreed_sink_interface():
    import inspect

    from langgraph_metarec.wp8_trace.interfaces import TraceEventSink

    for name in ("append", "open_instance", "close_instance", "finalize"):
        ours = inspect.signature(getattr(InMemoryTraceStore, name))
        theirs = inspect.signature(getattr(TraceEventSink, name))
        assert list(ours.parameters) == list(theirs.parameters), name
