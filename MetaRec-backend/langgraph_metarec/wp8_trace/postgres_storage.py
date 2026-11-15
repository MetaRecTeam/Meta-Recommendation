"""B4 · The durable trace store.

Same behaviour as ``InMemoryTraceStore``, kept across restarts. Pages are
built by the shared helpers in ``storage.py``, so the two stores cannot drift
apart in a corner nobody tests.

**Sequence allocation and append are one transaction** (DECISIONS.md R7). A
writer takes ``SELECT ... FOR UPDATE`` on the trace row, reads
``next_sequence``, inserts the event and bumps the counter before committing.
Two concurrent writers therefore serialise on that row: no duplicate sequence,
no gap, and no read-modify-write race. The counter lives on the trace row
rather than in a sequence object precisely so it is covered by that lock and
rolls back with a failed append.

Why async: the repository's Postgres access is async throughout
(``business_db``), and the trace API that serves pages is an async route.
A's collector talks to a sink synchronously, so the in-memory store stays the
sink during a run and this store is what the API reads and what survives a
restart. Wiring a write-behind flush is B5's job, not this module's.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import default_trace_config, effective_limits
from .interfaces import AppendResult, DraftEvent, TraceStatus
from .models import (
    DEFAULT_PAGE_EVENTS,
    MAX_PAGE_EVENTS,
    GraphDefinition,
    GraphInstance,
    GraphRunEventV1,
    GraphTraceConfigV1,
    GraphTraceV1,
)
from .storage import TraceNotFound, build_page, select_page, summarize


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """Postgres hands back timestamps in the session's timezone.

    ``timestamptz`` stores an instant, so the value is already right; only the
    offset differs. The contract's UtcDatetime requires a zero offset, so
    every timestamp is converted on the way out rather than relying on the
    server being configured for UTC.
    """
    if value is None:
        return None
    return value.astimezone(timezone.utc)


class PostgresTraceStore:
    """Durable implementation of the storage contract in ``storage.py``."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], *, clock=_utcnow) -> None:
        self._sessionmaker = sessionmaker
        self._clock = clock

    # -- lifecycle ---------------------------------------------------------

    async def start_trace(
        self,
        trace_id: str,
        debug_run_id: str,
        *,
        config: Optional[GraphTraceConfigV1] = None,
        definitions: Iterable[GraphDefinition] = (),
    ) -> None:
        """Open a trace. Opening the same one twice keeps what was captured."""
        config = config or default_trace_config()
        created = self._clock()
        async with self._sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO wp8_traces (
                            trace_id, debug_run_id, status, config, definitions,
                            redaction_policy_id, next_sequence, truncated,
                            dropped_event_count, stored_bytes, redacted_value_count,
                            truncated_value_count, created_at, expires_at
                        ) VALUES (
                            :trace_id, :debug_run_id, 'running', CAST(:config AS jsonb),
                            CAST(:definitions AS jsonb), :policy, 1, false, 0, 0, 0, 0,
                            :created_at, :expires_at
                        )
                        ON CONFLICT (trace_id) DO NOTHING
                        """
                    ),
                    {
                        "trace_id": trace_id,
                        "debug_run_id": debug_run_id,
                        "config": _json(config.model_dump(mode="json")),
                        "definitions": _json([d.model_dump(mode="json") for d in definitions]),
                        "policy": config.redaction_policy_id,
                        "created_at": created,
                        "expires_at": created + timedelta(days=config.retention_days),
                    },
                )

    async def finalize(self, trace_id: str, status: TraceStatus) -> None:
        """Set the terminal status once; a later call cannot reopen a trace."""
        async with self._sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE wp8_traces
                           SET status = :status, completed_at = :completed_at
                         WHERE trace_id = :trace_id AND status = 'running'
                        """
                    ),
                    {"trace_id": trace_id, "status": status, "completed_at": self._clock()},
                )

    # -- appending ---------------------------------------------------------

    async def append(self, trace_id: str, draft: DraftEvent) -> AppendResult:
        """Allocate the sequence and store the event in one transaction (R7)."""
        async with self._sessionmaker() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        text(
                            """
                            SELECT config, next_sequence, truncated, stored_bytes
                              FROM wp8_traces
                             WHERE trace_id = :trace_id
                               FOR UPDATE
                            """
                        ),
                        {"trace_id": trace_id},
                    )
                ).one_or_none()
                if row is None:
                    # A draft for a trace nobody opened. Creating one here
                    # would mean inventing its config and topology.
                    return AppendResult("dropped")

                config = GraphTraceConfigV1.model_validate(row.config)
                limits = effective_limits(config)

                if row.truncated:
                    await self._count_drop(session, trace_id)
                    return AppendResult("dropped")

                sequence = row.next_sequence
                event, size = self._build(trace_id, draft, sequence, limits["max_event_bytes"])
                if event is None:
                    await self._count_drop(session, trace_id)
                    return AppendResult("dropped")

                stored = await self._event_count(session, trace_id)
                if stored + 1 > limits["max_events"] or row.stored_bytes + size > limits["max_trace_bytes"]:
                    await self._write_marker(session, trace_id, draft, sequence)
                    await self._count_drop(session, trace_id)
                    return AppendResult("truncated", sequence)

                await self._insert(session, trace_id, event, size)
                await session.execute(
                    text(
                        """
                        UPDATE wp8_traces
                           SET next_sequence = :next,
                               stored_bytes = stored_bytes + :size,
                               redacted_value_count = redacted_value_count + :redacted,
                               truncated_value_count = truncated_value_count + :truncated,
                               started_at = COALESCE(started_at, :timestamp)
                         WHERE trace_id = :trace_id
                        """
                    ),
                    {
                        "trace_id": trace_id,
                        "next": sequence + 1,
                        "size": size,
                        "redacted": len(event.redaction.redacted_paths),
                        "truncated": len(event.redaction.truncated_paths),
                        "timestamp": event.timestamp_utc,
                    },
                )
                return AppendResult("accepted", sequence)

    def _build(self, trace_id: str, draft: DraftEvent, sequence: int, max_event_bytes: int):
        event_id = f"{trace_id}-ev-{sequence}"
        sized = draft.finalize(event_id=event_id, sequence=sequence, stored_bytes=0)
        size = len(sized.model_dump_json().encode("utf-8"))
        if size > max_event_bytes:
            return None, 0
        return draft.finalize(event_id=event_id, sequence=sequence, stored_bytes=size), size

    async def _insert(self, session: AsyncSession, trace_id: str,
                      event: GraphRunEventV1, size: int) -> None:
        await session.execute(
            text(
                """
                INSERT INTO wp8_trace_events (
                    trace_id, sequence, event_id, event_type, graph_instance_id,
                    node_id, timestamp_utc, event, stored_bytes
                ) VALUES (
                    :trace_id, :sequence, :event_id, :event_type, :graph_instance_id,
                    :node_id, :timestamp_utc, CAST(:event AS jsonb), :stored_bytes
                )
                """
            ),
            {
                "trace_id": trace_id,
                "sequence": event.sequence,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "graph_instance_id": event.graph_instance_id,
                "node_id": event.node_id,
                "timestamp_utc": event.timestamp_utc,
                "event": event.model_dump_json(),
                "stored_bytes": size,
            },
        )

    async def _event_count(self, session: AsyncSession, trace_id: str) -> int:
        return (
            await session.execute(
                text("SELECT count(*) FROM wp8_trace_events WHERE trace_id = :trace_id"),
                {"trace_id": trace_id},
            )
        ).scalar_one()

    async def _count_drop(self, session: AsyncSession, trace_id: str) -> None:
        await session.execute(
            text(
                "UPDATE wp8_traces SET dropped_event_count = dropped_event_count + 1"
                " WHERE trace_id = :trace_id"
            ),
            {"trace_id": trace_id},
        )

    async def _write_marker(self, session: AsyncSession, trace_id: str,
                            draft: DraftEvent, sequence: int) -> None:
        """One ``trace_truncated`` event; a collector fact, not a lifecycle span."""
        marker = GraphRunEventV1(
            schema_version="graph-run-event/1.0",
            event_id=f"{trace_id}-ev-{sequence}",
            trace_id=trace_id,
            debug_run_id=draft.debug_run_id,
            sequence=sequence,
            timestamp_utc=draft.timestamp_utc,
            monotonic_offset_ms=draft.monotonic_offset_ms,
            event_type="trace_truncated",
            status="incomplete",
            graph_definition_id=None,
            graph_instance_id=None,
            parent_graph_instance_id=None,
            node_id=None,
            run_id=None,
            parent_run_id=None,
            runnable_type="trace_system",
            duration_ms=None,
            input_summary=None,
            output_summary=None,
            state_diff=None,
            metadata=None,
            error=None,
            redaction={
                "policy_id": draft.policy_id,
                "redacted_paths": [],
                "truncated_paths": [],
                "stored_bytes": 0,
            },
        )
        size = len(marker.model_dump_json().encode("utf-8"))
        await self._insert(session, trace_id, marker, size)
        await session.execute(
            text(
                """
                UPDATE wp8_traces
                   SET truncated = true,
                       next_sequence = :next,
                       stored_bytes = stored_bytes + :size
                 WHERE trace_id = :trace_id
                """
            ),
            {"trace_id": trace_id, "next": sequence + 1, "size": size},
        )

    # -- instances ---------------------------------------------------------

    async def open_instance(self, trace_id: str, instance: GraphInstance) -> None:
        async with self._sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO wp8_trace_instances (
                            trace_id, graph_instance_id, graph_definition_id,
                            started_sequence, ended_sequence, instance
                        ) VALUES (
                            :trace_id, :graph_instance_id, :graph_definition_id,
                            :started_sequence, :ended_sequence, CAST(:instance AS jsonb)
                        )
                        ON CONFLICT (trace_id, graph_instance_id) DO NOTHING
                        """
                    ),
                    {
                        "trace_id": trace_id,
                        "graph_instance_id": instance.graph_instance_id,
                        "graph_definition_id": instance.graph_definition_id,
                        "started_sequence": instance.started_sequence,
                        "ended_sequence": instance.ended_sequence,
                        "instance": _json(instance.model_dump(mode="json")),
                    },
                )

    async def close_instance(self, trace_id: str, graph_instance_id: str,
                             ended_sequence: int) -> None:
        async with self._sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE wp8_trace_instances
                           SET ended_sequence = :ended_sequence,
                               -- The stored instance JSON is what read_page
                               -- returns, so it has to move with the column.
                               instance = jsonb_set(
                                   instance, '{ended_sequence}',
                                   to_jsonb(CAST(:ended_sequence AS integer)))
                         WHERE trace_id = :trace_id
                           AND graph_instance_id = :graph_instance_id
                        """
                    ),
                    {
                        "trace_id": trace_id,
                        "graph_instance_id": graph_instance_id,
                        "ended_sequence": ended_sequence,
                    },
                )

    # -- reading -----------------------------------------------------------

    async def read_page(
        self,
        trace_id: str,
        *,
        after_sequence: int = 0,
        limit: int = DEFAULT_PAGE_EVENTS,
    ) -> GraphTraceV1:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        limit = min(limit, MAX_PAGE_EVENTS)
        async with self._sessionmaker() as session:
            trace = (
                await session.execute(
                    text("SELECT * FROM wp8_traces WHERE trace_id = :trace_id"),
                    {"trace_id": trace_id},
                )
            ).one_or_none()
            if trace is None:
                raise TraceNotFound(trace_id)

            # One extra row tells us whether more remain without a count(*).
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT event FROM wp8_trace_events
                         WHERE trace_id = :trace_id AND sequence > :after
                         ORDER BY sequence
                         LIMIT :limit
                        """
                    ),
                    {"trace_id": trace_id, "after": after_sequence, "limit": limit + 1},
                )
            ).all()
            candidates = [GraphRunEventV1.model_validate(row.event) for row in rows]
            page, last, _ = select_page(candidates, after_sequence=after_sequence, limit=limit)
            has_more = len(rows) > limit or len(page) < len(candidates)

            instances = [
                GraphInstance.model_validate(row.instance)
                for row in (
                    await session.execute(
                        text(
                            "SELECT instance FROM wp8_trace_instances"
                            " WHERE trace_id = :trace_id ORDER BY started_sequence"
                        ),
                        {"trace_id": trace_id},
                    )
                ).all()
            ]

            config = GraphTraceConfigV1.model_validate(trace.config)
            return build_page(
                trace_id=trace.trace_id,
                debug_run_id=trace.debug_run_id,
                status=trace.status,
                config=config,
                definitions=[GraphDefinition.model_validate(d) for d in (trace.definitions or [])],
                instances=instances,
                events=page,
                after_sequence=after_sequence,
                next_after_sequence=last,
                has_more=has_more,
                summary=await self._summary(session, trace),
                redacted_value_count=trace.redacted_value_count,
                truncated_value_count=trace.truncated_value_count,
                truncated=trace.truncated,
                dropped_event_count=trace.dropped_event_count,
                created_at=_utc(trace.created_at),
                expires_at=_utc(trace.expires_at),
            )

    async def _summary(self, session: AsyncSession, trace) -> Any:
        """Counts over the whole trace, computed in the database, not the page."""
        counts = (
            await session.execute(
                text(
                    """
                    SELECT
                        count(DISTINCT (graph_instance_id, node_id))
                            FILTER (WHERE event_type = 'node_start' AND node_id IS NOT NULL)
                            AS visited,
                        count(*) FILTER (WHERE event_type = 'error') AS errors
                      FROM wp8_trace_events
                     WHERE trace_id = :trace_id
                    """
                ),
                {"trace_id": trace.trace_id},
            )
        ).one()
        started = _utc(trace.started_at) or _utc(trace.created_at)
        completed = _utc(trace.completed_at) if trace.status != "running" else None
        summary = summarize([], started_at=started, completed_at=completed)
        return summary.model_copy(
            update={"visited_nodes": counts.visited or 0, "error_count": counts.errors or 0}
        )

    # -- administration ----------------------------------------------------

    async def exists(self, trace_id: str) -> bool:
        async with self._sessionmaker() as session:
            return (
                await session.execute(
                    text("SELECT 1 FROM wp8_traces WHERE trace_id = :trace_id"),
                    {"trace_id": trace_id},
                )
            ).scalar_one_or_none() is not None

    async def trace_id_for_run(self, debug_run_id: str) -> Optional[str]:
        async with self._sessionmaker() as session:
            return (
                await session.execute(
                    text("SELECT trace_id FROM wp8_traces WHERE debug_run_id = :run"),
                    {"run": debug_run_id},
                )
            ).scalar_one_or_none()

    async def delete(self, trace_id: str) -> bool:
        """Remove one trace. Events and instances cascade with it."""
        async with self._sessionmaker() as session:
            async with session.begin():
                result = await session.execute(
                    text("DELETE FROM wp8_traces WHERE trace_id = :trace_id"),
                    {"trace_id": trace_id},
                )
                return result.rowcount > 0

    async def purge_expired(self, now: Optional[datetime] = None) -> int:
        """Delete traces past their retention. Returns how many went."""
        async with self._sessionmaker() as session:
            async with session.begin():
                result = await session.execute(
                    text("DELETE FROM wp8_traces WHERE expires_at <= :now"),
                    {"now": now or self._clock()},
                )
                return result.rowcount

    async def recover_running(self) -> int:
        """Mark traces left ``running`` by a crash as ``incomplete`` (R8).

        Run at start-up. The process that would have finalised them is gone,
        so nothing else will ever close them.
        """
        async with self._sessionmaker() as session:
            async with session.begin():
                result = await session.execute(
                    text(
                        "UPDATE wp8_traces SET status = 'incomplete', completed_at = :now"
                        " WHERE status = 'running'"
                    ),
                    {"now": self._clock()},
                )
                return result.rowcount
