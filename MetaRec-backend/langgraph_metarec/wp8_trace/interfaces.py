"""The seams between Member A's collector and Member B's projector and store.

The collector builds ``DraftEvent``s from callbacks; it never decides what is
safe (``SafeProjector`` does) and never assigns ``event_id`` or ``sequence``
(the ``TraceEventSink`` does, atomically with the append; DECISIONS.md R7).
``stubs.py`` has in-memory stand-ins until B's real ones exist (R16).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional, Protocol, Tuple

from .models import EventMetadata, GraphInstance, GraphRunEventV1, PayloadSummary, SafeError, StateDiff

AppendStatus = Literal["accepted", "truncated", "dropped"]
TraceStatus = Literal["running", "completed", "failed", "cancelled", "incomplete"]


@dataclass(frozen=True)
class Projection:
    """What the projector lets through for one event. Raw values never leave it."""

    input_summary: Optional[PayloadSummary] = None
    output_summary: Optional[PayloadSummary] = None
    state_diff: Optional[StateDiff] = None
    metadata: Optional[EventMetadata] = None
    error: Optional[SafeError] = None
    redacted_paths: Tuple[str, ...] = ()
    truncated_paths: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DraftEvent:
    """A GraphRunEventV1 before the sink gives it an event_id, sequence and stored size."""

    trace_id: str
    debug_run_id: str
    timestamp_utc: datetime
    monotonic_offset_ms: float
    event_type: str
    status: str
    graph_definition_id: Optional[str]
    graph_instance_id: Optional[str]
    parent_graph_instance_id: Optional[str]
    node_id: Optional[str]
    run_id: Optional[str]
    parent_run_id: Optional[str]
    runnable_type: str
    duration_ms: Optional[float]
    policy_id: str
    projection: Projection = field(default_factory=Projection)

    def finalize(self, *, event_id: str, sequence: int, stored_bytes: int) -> GraphRunEventV1:
        """The contract event; raises if the draft breaks the event contract."""
        fields = {k: v for k, v in asdict(self).items() if k not in ("policy_id", "projection")}
        p = self.projection
        return GraphRunEventV1(
            schema_version="graph-run-event/1.0",
            event_id=event_id,
            sequence=sequence,
            **fields,
            input_summary=p.input_summary,
            output_summary=p.output_summary,
            state_diff=p.state_diff,
            metadata=p.metadata,
            error=p.error,
            redaction={
                "policy_id": self.policy_id,
                "redacted_paths": list(p.redacted_paths),
                "truncated_paths": list(p.truncated_paths),
                "stored_bytes": stored_bytes,
            },
        )


@dataclass(frozen=True)
class AppendResult:
    status: AppendStatus
    sequence: Optional[int] = None  # set when the event was stored (accepted or the truncation marker)


class SafeProjector(Protocol):
    """Member B: allowlist -> deny rules -> patterns -> bounds, before anything is stored.

    Called once per event. ``node_input``/``output`` are raw graph or node state;
    child spans (tool, LLM) pass only ``raw_name``, never prompts or tool input.
    """

    policy_id: str

    def project(
        self,
        *,
        event_type: str,
        runnable_type: str,
        graph_definition_id: str,
        node_id: Optional[str],
        node_input: Any = None,
        output: Any = None,
        error: Optional[BaseException] = None,
        raw_name: Optional[str] = None,
    ) -> Projection: ...


class TraceEventSink(Protocol):
    """Member B: atomic sequence + append, caps and truncation, instances, final status."""

    def append(self, trace_id: str, draft: DraftEvent) -> AppendResult: ...

    def open_instance(self, trace_id: str, instance: GraphInstance) -> None: ...

    def close_instance(self, trace_id: str, graph_instance_id: str, ended_sequence: int) -> None: ...

    def finalize(self, trace_id: str, status: TraceStatus) -> None: ...
