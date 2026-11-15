"""In-memory stand-ins for Member B's projector and store (DECISIONS.md R16).

They let the collector be built and tested before redaction and Postgres
exist. ``NullProjector`` lets nothing through except a generic safe error;
``InMemoryTraceSink`` validates every event against the contract and keeps it
in a list. Neither is used by production code.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Optional

from .interfaces import AppendResult, DraftEvent, Projection, TraceStatus
from .models import GraphInstance, GraphRunEventV1, SafeError

DEFAULT_POLICY_ID = "metarec-debug-safe-v1"


class NullProjector:
    """No summaries, no diff, no metadata; errors become the draft R10 codes."""

    def __init__(self, policy_id: str = DEFAULT_POLICY_ID) -> None:
        self.policy_id = policy_id

    def project(self, *, event_type: str, runnable_type: str, graph_definition_id: str, node_id: Optional[str],
                node_input: Any = None, output: Any = None, error: Optional[BaseException] = None,
                raw_name: Optional[str] = None) -> Projection:
        if error is None:
            return Projection()
        cancelled = isinstance(error, asyncio.CancelledError)
        return Projection(error=SafeError(
            code="cancelled" if cancelled else "execution_failed",
            error_class=type(error).__name__,
            safe_message="Run was cancelled" if cancelled else "Run failed",
            retryable=False,
        ))


class InMemoryTraceSink:
    """One lock per sink: sequence allocation and append happen together."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: Dict[str, List[GraphRunEventV1]] = {}
        self.instances: Dict[str, Dict[str, GraphInstance]] = {}
        self.status: Dict[str, TraceStatus] = {}

    def append(self, trace_id: str, draft: DraftEvent) -> AppendResult:
        with self._lock:
            events = self.events.setdefault(trace_id, [])
            sequence = len(events) + 1
            event_id = f"{trace_id}-ev-{sequence}"
            sized = draft.finalize(event_id=event_id, sequence=sequence, stored_bytes=0)
            event = draft.finalize(event_id=event_id, sequence=sequence,
                                   stored_bytes=len(sized.model_dump_json().encode("utf-8")))
            events.append(event)
            return AppendResult("accepted", sequence)

    def open_instance(self, trace_id: str, instance: GraphInstance) -> None:
        with self._lock:
            self.instances.setdefault(trace_id, {})[instance.graph_instance_id] = instance

    def close_instance(self, trace_id: str, graph_instance_id: str, ended_sequence: int) -> None:
        with self._lock:
            instances = self.instances[trace_id]
            instances[graph_instance_id] = instances[graph_instance_id].model_copy(
                update={"ended_sequence": ended_sequence})

    def finalize(self, trace_id: str, status: TraceStatus) -> None:
        with self._lock:
            self.status[trace_id] = status
