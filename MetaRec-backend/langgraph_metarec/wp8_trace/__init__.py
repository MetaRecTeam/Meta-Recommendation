"""WP8 LangGraph trace support (opt-in, admin-only, observation only).

Nothing in this package is imported by the recommendation path unless WP8
tracing is wired in, and nothing here may change recommendation behaviour.
"""
from .collector import TraceCollector, new_trace_context
from .interfaces import AppendResult, DraftEvent, Projection, SafeProjector, TraceEventSink
from .invocation import TraceContext, invoke_with_wp8_trace, trace_kwargs
from .models import GraphDefinition, GraphInstance, GraphRunEventV1, TopologyEdge, TopologyNode
from .topology import (
    RegisteredGraph,
    extract_definition,
    graph_definition_ids,
    registered_graph,
    topology_definitions,
)

__all__ = [
    "AppendResult",
    "DraftEvent",
    "Projection",
    "SafeProjector",
    "TraceCollector",
    "TraceEventSink",
    "new_trace_context",
    "GraphDefinition",
    "GraphInstance",
    "GraphRunEventV1",
    "RegisteredGraph",
    "TopologyEdge",
    "TopologyNode",
    "TraceContext",
    "extract_definition",
    "graph_definition_ids",
    "invoke_with_wp8_trace",
    "registered_graph",
    "topology_definitions",
    "trace_kwargs",
]
