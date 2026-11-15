"""B3 · The trace store: atomic sequencing, caps, truncation and paged reads.

``InMemoryTraceStore`` implements ``interfaces.TraceEventSink`` (DECISIONS.md
R7 and R16). It is the reference implementation of the storage rules; the
Postgres store (B4) has to behave identically, and shares this module's tests.

Three rules this store owns:

**Sequence is allocated with the append, not before it.** One lock covers
both, so two concurrent appends can never take the same number and no number
is ever skipped. The collector sends drafts carrying timing only.

**Caps are enforced on the way in, never on the way out.** Once a trace is at
its event, event-size or total-size limit, one ``trace_truncated`` marker is
written and everything after it is counted in ``dropped_event_count``. A
reader therefore always sees a bounded trace that says how much is missing,
rather than an unbounded one or a silent cut.

**A page is a window over stored events, not a copy of the trace.**
``read_page`` returns at most ``limit`` events after a cursor, with
``has_more`` and the cursor to pass next.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Mapping, Optional

from .config import DEFAULT_POLICY_ID, default_trace_config, effective_limits
from .interfaces import AppendResult, DraftEvent, TraceStatus
from .models import (
    DEFAULT_PAGE_EVENTS,
    MAX_PAGE_BYTES,
    MAX_PAGE_EVENTS,
    EventWindow,
    GraphDefinition,
    GraphInstance,
    GraphRunEventV1,
    GraphTraceConfigV1,
    GraphTraceV1,
    RedactionSummary,
    TraceConfigSnapshot,
    TraceSummary,
    TraceTopology,
)


class TraceNotFound(KeyError):
    """No trace with that id, or it has expired and been purged."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _TraceRecord:
    """One trace's stored state. Only the store touches this."""

    def __init__(
        self,
        *,
        trace_id: str,
        debug_run_id: str,
        config: GraphTraceConfigV1,
        definitions: Iterable[GraphDefinition],
        created_at: datetime,
    ) -> None:
        self.trace_id = trace_id
        self.debug_run_id = debug_run_id
        self.config = config
        self.limits = effective_limits(config)
        self.definitions: List[GraphDefinition] = list(definitions)
        self.instances: Dict[str, GraphInstance] = {}
        self.events: List[GraphRunEventV1] = []
        self.status: TraceStatus = "running"
        self.truncated = False
        self.dropped_event_count = 0
        self.stored_bytes = 0
        self.redacted_value_count = 0
        self.truncated_value_count = 0
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        self.created_at = created_at
        self.expires_at = created_at + timedelta(days=config.retention_days)
        self.next_sequence = 1


class InMemoryTraceStore:
    """A complete, bounded trace store that keeps everything in this process.

    Useful on its own for tests and for a single-process deployment, and it is
    the behaviour the Postgres store is checked against.
    """

    def __init__(self, *, clock=_utcnow) -> None:
        self._lock = threading.Lock()
        self._traces: Dict[str, _TraceRecord] = {}
        self._clock = clock

    # -- lifecycle ---------------------------------------------------------

    def start_trace(
        self,
        trace_id: str,
        debug_run_id: str,
        *,
        config: Optional[GraphTraceConfigV1] = None,
        definitions: Iterable[GraphDefinition] = (),
    ) -> None:
        """Open a trace. Starting the same trace twice is a no-op, not an error.

        A retried debug run must not wipe what was already captured.
        """
        with self._lock:
            if trace_id in self._traces:
                return
            self._traces[trace_id] = _TraceRecord(
                trace_id=trace_id,
                debug_run_id=debug_run_id,
                config=config or default_trace_config(),
                definitions=definitions,
                created_at=self._clock(),
            )

    def finalize(self, trace_id: str, status: TraceStatus) -> None:
        """Set the terminal status once. A later call cannot reopen a trace."""
        with self._lock:
            record = self._traces.get(trace_id)
            if record is None or record.status != "running":
                return
            record.status = status
            record.completed_at = self._clock()

    # -- appending ---------------------------------------------------------

    def append(self, trace_id: str, draft: DraftEvent) -> AppendResult:
        """Assign sequence and event_id and store the event, atomically (R7)."""
        with self._lock:
            record = self._traces.get(trace_id)
            if record is None:
                # A draft for a trace nobody opened is dropped rather than
                # creating one: the config and topology would be guesses.
                return AppendResult("dropped")

            if record.truncated:
                record.dropped_event_count += 1
                return AppendResult("dropped")

            event, size = self._build(record, draft, record.next_sequence)
            if event is None:
                # Too big on its own. It cannot be made smaller here without
                # editing what the projector decided, so it is dropped whole.
                record.dropped_event_count += 1
                return AppendResult("dropped")

            over_count = len(record.events) + 1 > record.limits["max_events"]
            over_bytes = record.stored_bytes + size > record.limits["max_trace_bytes"]
            if over_count or over_bytes:
                marker_sequence = self._write_marker(record, draft)
                record.dropped_event_count += 1
                return AppendResult("truncated", marker_sequence)

            self._store(record, event, size)
            return AppendResult("accepted", event.sequence)

    def _build(self, record: _TraceRecord, draft: DraftEvent, sequence: int):
        """Finalize a draft and measure it. Returns (event, bytes) or (None, 0)."""
        event_id = f"{record.trace_id}-ev-{sequence}"
        sized = draft.finalize(event_id=event_id, sequence=sequence, stored_bytes=0)
        size = len(sized.model_dump_json().encode("utf-8"))
        if size > record.limits["max_event_bytes"]:
            return None, 0
        return draft.finalize(event_id=event_id, sequence=sequence, stored_bytes=size), size

    def _store(self, record: _TraceRecord, event: GraphRunEventV1, size: int) -> None:
        record.events.append(event)
        record.next_sequence = event.sequence + 1
        record.stored_bytes += size
        record.redacted_value_count += len(event.redaction.redacted_paths)
        record.truncated_value_count += len(event.redaction.truncated_paths)
        if record.started_at is None:
            record.started_at = event.timestamp_utc

    def _write_marker(self, record: _TraceRecord, draft: DraftEvent) -> Optional[int]:
        """One ``trace_truncated`` event, then nothing else is stored.

        The marker is a collector-level fact, not a lifecycle span, so it
        carries no node_id, run_id or parent_run_id (the event matrix forbids
        them). It replaces the event that would not fit, so the stored trace
        never ends on a silent cut.
        """
        record.truncated = True
        sequence = record.next_sequence
        marker = GraphRunEventV1(
            schema_version="graph-run-event/1.0",
            event_id=f"{record.trace_id}-ev-{sequence}",
            trace_id=record.trace_id,
            debug_run_id=record.debug_run_id,
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
                "policy_id": draft.policy_id or DEFAULT_POLICY_ID,
                "redacted_paths": [],
                "truncated_paths": [],
                "stored_bytes": 0,
            },
        )
        size = len(marker.model_dump_json().encode("utf-8"))
        record.events.append(marker)
        record.next_sequence = sequence + 1
        record.stored_bytes += size
        return sequence

    # -- instances ---------------------------------------------------------

    def open_instance(self, trace_id: str, instance: GraphInstance) -> None:
        with self._lock:
            record = self._traces.get(trace_id)
            if record is None:
                return
            record.instances[instance.graph_instance_id] = instance

    def close_instance(self, trace_id: str, graph_instance_id: str, ended_sequence: int) -> None:
        with self._lock:
            record = self._traces.get(trace_id)
            if record is None:
                return
            existing = record.instances.get(graph_instance_id)
            if existing is None:
                return
            record.instances[graph_instance_id] = existing.model_copy(
                update={"ended_sequence": ended_sequence}
            )

    # -- reading -----------------------------------------------------------

    def read_page(
        self,
        trace_id: str,
        *,
        after_sequence: int = 0,
        limit: int = DEFAULT_PAGE_EVENTS,
    ) -> GraphTraceV1:
        """One page of a trace, as the contract page model.

        ``after_sequence`` is a cursor, not an index: it is the last sequence
        the caller already has. A page is also capped by bytes, so a page of
        large events returns fewer than ``limit`` rather than an oversized
        response; ``has_more`` then tells the caller to come back.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        limit = min(limit, MAX_PAGE_EVENTS)
        with self._lock:
            record = self._traces.get(trace_id)
            if record is None:
                raise TraceNotFound(trace_id)

            page, last, has_more = select_page(
                record.events, after_sequence=after_sequence, limit=limit
            )
            return build_page(
                trace_id=record.trace_id,
                debug_run_id=record.debug_run_id,
                status=record.status,
                config=record.config,
                definitions=record.definitions,
                instances=list(record.instances.values()),
                events=page,
                after_sequence=after_sequence,
                next_after_sequence=last,
                has_more=has_more,
                summary=self._summary(record),
                redacted_value_count=record.redacted_value_count,
                truncated_value_count=record.truncated_value_count,
                truncated=record.truncated,
                dropped_event_count=record.dropped_event_count,
                created_at=record.created_at,
                expires_at=record.expires_at,
            )

    def _summary(self, record: _TraceRecord) -> TraceSummary:
        return summarize(
            record.events,
            started_at=record.started_at or record.created_at,
            completed_at=record.completed_at if record.status != "running" else None,
        )

    # -- administration ----------------------------------------------------

    def exists(self, trace_id: str) -> bool:
        with self._lock:
            return trace_id in self._traces

    def trace_id_for_run(self, debug_run_id: str) -> Optional[str]:
        with self._lock:
            for record in self._traces.values():
                if record.debug_run_id == debug_run_id:
                    return record.trace_id
        return None

    def delete(self, trace_id: str) -> bool:
        with self._lock:
            return self._traces.pop(trace_id, None) is not None

    def purge_expired(self, now: Optional[datetime] = None) -> int:
        """Drop traces past their retention. Returns how many went."""
        moment = now or self._clock()
        with self._lock:
            expired = [tid for tid, rec in self._traces.items() if rec.expires_at <= moment]
            for trace_id in expired:
                del self._traces[trace_id]
            return len(expired)

    def recover_running(self) -> int:
        """Mark traces left ``running`` by a crash as ``incomplete``.

        The only status this store assigns on its own, at start-up, because it
        is the one case the collector cannot report: the process that would
        have finalised the trace is gone (DECISIONS.md R8).
        """
        with self._lock:
            count = 0
            for record in self._traces.values():
                if record.status == "running":
                    record.status = "incomplete"
                    record.completed_at = self._clock()
                    count += 1
            return count


def select_page(
    events: List[GraphRunEventV1],
    *,
    after_sequence: int,
    limit: int,
    max_page_bytes: int = MAX_PAGE_BYTES,
) -> tuple[List[GraphRunEventV1], int, bool]:
    """The events for one page, the cursor to pass next, and whether more remain.

    A page is capped by bytes as well as by count, so a page of large events
    returns fewer than ``limit`` rather than an oversized response. At least
    one event is always returned when any remain, otherwise a single event
    larger than the page budget would stall the cursor forever.
    """
    remaining = [event for event in events if event.sequence > after_sequence]
    page: List[GraphRunEventV1] = []
    page_bytes = 0
    for event in remaining[:limit]:
        size = event.redaction.stored_bytes or len(event.model_dump_json().encode("utf-8"))
        if page and page_bytes + size > max_page_bytes:
            break
        page.append(event)
        page_bytes += size
    last = page[-1].sequence if page else after_sequence
    return page, last, len(remaining) > len(page)


def build_page(
    *,
    trace_id: str,
    debug_run_id: str,
    status: TraceStatus,
    config: GraphTraceConfigV1,
    definitions: Iterable[GraphDefinition],
    instances: Iterable[GraphInstance],
    events: List[GraphRunEventV1],
    after_sequence: int,
    next_after_sequence: int,
    has_more: bool,
    summary: TraceSummary,
    redacted_value_count: int,
    truncated_value_count: int,
    truncated: bool,
    dropped_event_count: int,
    created_at: datetime,
    expires_at: datetime,
) -> GraphTraceV1:
    """One page, in the contract shape. Every store returns pages through here.

    Two stores that build the page separately will drift apart in some corner
    nobody tests. There is one builder, so they cannot.
    """
    return GraphTraceV1(
        schema_version="graph-trace/1.0",
        trace_id=trace_id,
        debug_run_id=debug_run_id,
        status=status,
        config_snapshot=TraceConfigSnapshot(
            redaction_policy_id=config.redaction_policy_id,
            capture=config.capture,
            limits=config.limits,
            retention_days=config.retention_days,
        ),
        topology=TraceTopology(definitions=list(definitions), instances=list(instances)),
        events=events,
        event_window=EventWindow(
            after_sequence=after_sequence,
            returned=len(events),
            next_after_sequence=next_after_sequence,
            has_more=has_more,
        ),
        summary=summary,
        redaction_summary=RedactionSummary(
            redacted_value_count=redacted_value_count,
            truncated_value_count=truncated_value_count,
        ),
        truncated=truncated,
        dropped_event_count=dropped_event_count,
        created_at=created_at,
        expires_at=expires_at,
    )


def summarize(
    events: Iterable[GraphRunEventV1],
    *,
    started_at: datetime,
    completed_at: Optional[datetime],
) -> TraceSummary:
    """Whole-trace counts. Shared so both stores count the same things."""
    events = list(events)
    duration = None
    if completed_at is not None:
        duration = max((completed_at - started_at).total_seconds() * 1000.0, 0.0)
    visited = {
        (event.graph_instance_id, event.node_id)
        for event in events
        if event.event_type == "node_start" and event.node_id is not None
    }
    return TraceSummary(
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration,
        visited_nodes=len(visited),
        error_count=sum(1 for event in events if event.event_type == "error"),
    )
