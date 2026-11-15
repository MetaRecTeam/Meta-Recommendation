"""Shared builders for the trace store tests.

B3's in-memory store and B4's Postgres store must behave identically, so the
behaviour tests are written against these builders and run twice.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from langgraph_metarec.wp8_trace.interfaces import DraftEvent, Projection
from langgraph_metarec.wp8_trace.models import (
    GraphDefinition,
    GraphInstance,
    PayloadSummary,
    TopologyEdge,
    TopologyNode,
)
from langgraph_metarec.wp8_trace.redaction import POLICY_ID

GRAPH = "generic-domain@v1"
T0 = datetime(2026, 8, 20, 10, 30, tzinfo=timezone.utc)


def definition() -> GraphDefinition:
    return GraphDefinition(
        graph_definition_id=GRAPH,
        name="Generic domain graph",
        nodes=[
            TopologyNode(node_id="__start__", label="Start", kind="start"),
            TopologyNode(node_id="candidate_gather", label="Candidate gather", kind="node"),
        ],
        edges=[TopologyEdge(source="__start__", target="candidate_gather",
                            kind="normal", label=None)],
    )


def instance(graph_instance_id: str = "gi-1", started_sequence: int = 1) -> GraphInstance:
    return GraphInstance(
        graph_instance_id=graph_instance_id,
        graph_definition_id=GRAPH,
        parent_graph_instance_id=None,
        parent_node_run_id=None,
        namespace=[],
        started_sequence=started_sequence,
        ended_sequence=None,
    )


def draft(
    *,
    trace_id: str = "tr-1",
    debug_run_id: str = "run-1",
    event_type: str = "node_start",
    status: str = "running",
    node_id: Optional[str] = "candidate_gather",
    run_id: Optional[str] = "r-1",
    parent_run_id: Optional[str] = "r-0",
    runnable_type: str = "graph_node",
    offset_ms: float = 0.0,
    duration_ms: Optional[float] = None,
    projection: Optional[Projection] = None,
) -> DraftEvent:
    return DraftEvent(
        trace_id=trace_id,
        debug_run_id=debug_run_id,
        timestamp_utc=T0 + timedelta(milliseconds=offset_ms),
        monotonic_offset_ms=offset_ms,
        event_type=event_type,
        status=status,
        graph_definition_id=GRAPH,
        graph_instance_id="gi-1",
        parent_graph_instance_id=None,
        node_id=node_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        runnable_type=runnable_type,
        duration_ms=duration_ms,
        policy_id=POLICY_ID,
        projection=projection or Projection(),
    )


def graph_start(**kwargs) -> DraftEvent:
    kwargs.setdefault("event_type", "graph_start")
    kwargs.setdefault("runnable_type", "graph")
    kwargs.setdefault("node_id", None)
    kwargs.setdefault("parent_run_id", None)
    kwargs.setdefault("run_id", "r-0")
    return draft(**kwargs)


def node_end(**kwargs) -> DraftEvent:
    kwargs.setdefault("event_type", "node_end")
    kwargs.setdefault("status", "completed")
    kwargs.setdefault("duration_ms", 12.5)
    return draft(**kwargs)


def fat_projection(paths: int = 28, chars: int = 500) -> Projection:
    """A projection big enough to matter for the byte caps.

    `redacted_paths` is a bounded list of bounded strings (50 x 512), so the
    defaults sit just under the 16 KB per-event cap: enough that a page of
    them is cut by bytes before it is cut by count.
    """
    return Projection(
        input_summary=PayloadSummary(
            keys=[f"k{i}" for i in range(min(paths, 50))],
            bytes=paths * chars,
            value_types={f"k{i}": "str" for i in range(min(paths, 50))},
            item_count=paths,
            omitted_count=0,
        ),
        redacted_paths=tuple(f"p{i}".ljust(chars, "x") for i in range(paths)),
    )
