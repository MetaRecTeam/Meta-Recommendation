"""Topology registry: the possible nodes and edges of the five MetaRec graphs.

Definitions are extracted from the compiled graphs themselves, never written
by hand, so they cannot drift from the code silently: the snapshot test in
tests/test_wp8_topology.py fails when a graph changes, and the fix is to bump
that graph's ``@vN`` and refresh its snapshot.

Extraction only compiles a graph with inert adapters; it never invokes one.
Graph modules are imported lazily so the graph runners can later import the
WP8 invocation helper without an import cycle.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from .models import GraphDefinition, TopologyEdge, TopologyNode

START_NODE = "__start__"
END_NODE = "__end__"


@dataclass(frozen=True)
class RegisteredGraph:
    definition: GraphDefinition
    # Conditional-edge router functions. Callbacks report them as child runs
    # of the node they leave from; they are edge evaluations, not node visits.
    routers: FrozenSet[str]
    fingerprint: str


async def _never_called(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("topology extraction compiles graphs but never runs them")


def _build_request_orchestrator() -> Any:
    from langgraph_metarec.graphs.request_orchestrator import (
        RequestOrchestratorAdapters,
        build_request_orchestrator_graph,
    )

    adapters = RequestOrchestratorAdapters(_never_called, _never_called, _never_called, _never_called)
    return build_request_orchestrator_graph(adapters)


def _build_routing() -> Any:
    from langgraph_metarec.graphs.routing_graph import build_routing_graph

    return build_routing_graph()


def _build_task() -> Any:
    from langgraph_metarec.graphs.task_graph import TaskGraphAdapters, build_task_graph

    return build_task_graph(TaskGraphAdapters(_never_called, _never_called), checkpointer=None)


def _build_generic() -> Any:
    from langgraph_metarec.graphs.generic_graph import build_generic_domain_graph

    return build_generic_domain_graph()


def _build_restaurant() -> Any:
    from langgraph_metarec.graphs.restaurant_graph import build_restaurant_graph

    return build_restaurant_graph(client=None, summary_model="", planning_model="")


# graph_definition_id -> (display name, builder). Bump @vN when a node or edge
# of that graph is added, removed or renamed.
_GRAPHS: Dict[str, Tuple[str, Callable[[], Any]]] = {
    "request-orchestrator@v1": ("Request orchestrator", _build_request_orchestrator),
    "routing-graph@v1": ("Routing graph", _build_routing),
    "task-graph@v1": ("Task graph", _build_task),
    "generic-domain@v1": ("Generic domain graph", _build_generic),
    "restaurant-domain@v1": ("Restaurant domain graph", _build_restaurant),
}


def _node_label(node_id: str) -> str:
    if node_id == START_NODE:
        return "Start"
    if node_id == END_NODE:
        return "End"
    return node_id.replace("_", " ").capitalize()


def _node_kind(node_id: str) -> str:
    return {START_NODE: "start", END_NODE: "end"}.get(node_id, "node")


def _branch_ends(compiled: Any) -> Dict[str, Dict[str, Any]]:
    builder = getattr(compiled, "builder", None)
    return dict(getattr(builder, "branches", None) or {})


def _conditional_labels(compiled: Any) -> Dict[Tuple[str, str], Optional[str]]:
    """(source, target) -> route key(s) the router returns to take that edge."""
    keys: Dict[Tuple[str, str], List[str]] = {}
    for source, branches in _branch_ends(compiled).items():
        for spec in branches.values():
            for key, target in (getattr(spec, "ends", None) or {}).items():
                keys.setdefault((source, str(target)), []).append(str(key))
    return {edge: "|".join(sorted(set(names))) for edge, names in keys.items()}


def edge_routers(compiled: Any) -> FrozenSet[str]:
    return frozenset(name for branches in _branch_ends(compiled).values() for name in branches)


def extract_definition(compiled: Any, graph_definition_id: str, name: str) -> GraphDefinition:
    """Read the possible topology of a compiled graph. Does not run it."""
    drawable = compiled.get_graph()
    node_ids = list(drawable.nodes)
    order = {node_id: index for index, node_id in enumerate(node_ids)}
    labels = _conditional_labels(compiled)
    edges = sorted(drawable.edges, key=lambda e: (order[e.source], order[e.target]))
    return GraphDefinition(
        graph_definition_id=graph_definition_id,
        name=name,
        nodes=[TopologyNode(node_id=n, label=_node_label(n), kind=_node_kind(n)) for n in node_ids],
        edges=[
            TopologyEdge(
                source=e.source,
                target=e.target,
                kind="conditional" if e.conditional else "normal",
                label=labels.get((e.source, e.target)) if e.conditional else None,
            )
            for e in edges
        ],
    )


def topology_fingerprint(definition: GraphDefinition) -> str:
    canonical = json.dumps(definition.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def graph_definition_ids() -> List[str]:
    return list(_GRAPHS)


@lru_cache(maxsize=None)
def registered_graph(graph_definition_id: str) -> RegisteredGraph:
    try:
        name, build = _GRAPHS[graph_definition_id]
    except KeyError:
        raise KeyError(f"unknown graph_definition_id: {graph_definition_id}") from None
    compiled = build()
    definition = extract_definition(compiled, graph_definition_id, name)
    return RegisteredGraph(definition, edge_routers(compiled), topology_fingerprint(definition))


def topology_definitions() -> List[GraphDefinition]:
    return [registered_graph(graph_id).definition for graph_id in _GRAPHS]


def matches_registered(compiled: Any, graph_definition_id: str) -> bool:
    """True when a compiled graph still has the registered topology."""
    registered = registered_graph(graph_definition_id)
    current = extract_definition(compiled, graph_definition_id, registered.definition.name)
    return topology_fingerprint(current) == registered.fingerprint
