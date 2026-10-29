"""WP8 topology registry: possible nodes/edges of the five graphs, frozen."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from langgraph_metarec.wp8_trace.models import GraphDefinition, GraphInstance, TopologyEdge
from langgraph_metarec.wp8_trace.topology import (
    _GRAPHS,
    END_NODE,
    START_NODE,
    extract_definition,
    graph_definition_ids,
    matches_registered,
    registered_graph,
    topology_definitions,
    topology_fingerprint,
)

SNAPSHOT_DIR = Path(__file__).resolve().parent / "fixtures" / "wp8" / "topology"
REFRESH_HINT = (
    "topology changed: bump this graph's @vN in langgraph_metarec/wp8_trace/topology.py, "
    "then run scripts/wp8_topology_snapshot.py"
)

# docs/wp8/GRAPH_EXECUTION_FLOW.md §4, in topological order.
DOCUMENTED_NODES = {
    "request-orchestrator@v1": ["intention", "collect_confirm", "routing", "domain_dispatch", "result"],
    "routing-graph@v1": ["domain_classification", "retry_domain", "single_domain", "multi_domain", "itinerary", "domain_error"],
    "task-graph@v1": ["task_started", "domain_graph_dispatch", "result_projection"],
    "generic-domain@v1": ["candidate_gather", "normalize_and_rank", "recommendation_result"],
    "restaurant-domain@v1": ["candidate_gather", "rerank_and_summarize", "validation_and_calibration", "recommendation_result"],
}


def _business_nodes(definition):
    return [n.node_id for n in definition.nodes if n.kind == "node"]


@pytest.mark.backend_unit
def test_registry_covers_the_five_production_graphs():
    assert graph_definition_ids() == list(DOCUMENTED_NODES)


@pytest.mark.backend_unit
@pytest.mark.parametrize("graph_definition_id", list(DOCUMENTED_NODES))
def test_definition_matches_frozen_snapshot(graph_definition_id):
    snapshot = json.loads((SNAPSHOT_DIR / f"{graph_definition_id}.json").read_text())

    assert registered_graph(graph_definition_id).definition.model_dump(mode="json") == snapshot, REFRESH_HINT


@pytest.mark.backend_unit
def test_no_stale_snapshots():
    assert sorted(p.stem for p in SNAPSHOT_DIR.glob("*.json")) == sorted(DOCUMENTED_NODES)


@pytest.mark.backend_unit
@pytest.mark.parametrize("graph_definition_id", list(DOCUMENTED_NODES))
def test_node_ids_match_the_documented_call_map(graph_definition_id):
    definition = registered_graph(graph_definition_id).definition

    assert _business_nodes(definition) == DOCUMENTED_NODES[graph_definition_id]


@pytest.mark.backend_unit
@pytest.mark.parametrize("definition", topology_definitions(), ids=lambda d: d.graph_definition_id)
def test_definition_is_well_formed(definition):
    node_ids = [n.node_id for n in definition.nodes]
    assert len(node_ids) == len(set(node_ids))
    assert [n.node_id for n in definition.nodes if n.kind == "start"] == [START_NODE]
    assert [n.node_id for n in definition.nodes if n.kind == "end"] == [END_NODE]
    for edge in definition.edges:
        assert edge.source in node_ids and edge.target in node_ids
        if edge.kind == "conditional":
            assert edge.label, f"conditional edge {edge.source}->{edge.target} has no route key"
        else:
            assert edge.label is None


@pytest.mark.backend_unit
def test_conditional_branches_are_the_possible_paths_only():
    routing = registered_graph("routing-graph@v1").definition
    conditional = {(e.source, e.target) for e in routing.edges if e.kind == "conditional"}

    assert conditional == {
        ("domain_classification", "retry_domain"),
        ("domain_classification", "single_domain"),
        ("domain_classification", "multi_domain"),
        ("domain_classification", "itinerary"),
        ("retry_domain", "single_domain"),
        ("retry_domain", "multi_domain"),
        ("retry_domain", "domain_error"),
    }


@pytest.mark.backend_unit
def test_edge_routers_are_named_and_are_not_nodes():
    # The D0 probe saw these as child chain runs of nodes; the collector must
    # recognise them as edge evaluations (docs/wp8/parentage-baseline/REPORT.md §5.8).
    expected = {
        "request-orchestrator@v1": {"_route_after_collect_confirm"},
        "routing-graph@v1": {"route_after_classification", "route_after_retry"},
        "task-graph@v1": set(),
        "generic-domain@v1": set(),
        "restaurant-domain@v1": set(),
    }
    for graph_id, routers in expected.items():
        registered = registered_graph(graph_id)
        assert set(registered.routers) == routers
        assert not routers & {n.node_id for n in registered.definition.nodes}


@pytest.mark.backend_unit
def test_topology_does_not_depend_on_adapters():
    from langgraph_metarec.graphs.generic_graph import GenericGraphAdapters, build_generic_domain_graph
    from langgraph_metarec.tool_registry import ToolRegistry

    graph = build_generic_domain_graph(adapters=GenericGraphAdapters(tool_registry=ToolRegistry()))

    assert matches_registered(graph, "generic-domain@v1")


@pytest.mark.backend_unit
def test_drift_is_detected():
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class S(TypedDict):
        x: int

    def step(state):
        return state

    graph = StateGraph(S)
    for node in ("task_started", "domain_graph_dispatch", "new_node", "result_projection"):
        graph.add_node(node, step)
    graph.add_edge(START, "task_started")
    graph.add_edge("task_started", "domain_graph_dispatch")
    graph.add_edge("domain_graph_dispatch", "new_node")
    graph.add_edge("new_node", "result_projection")
    graph.add_edge("result_projection", END)

    assert not matches_registered(graph.compile(), "task-graph@v1")


@pytest.mark.backend_unit
def test_definition_ids_and_fingerprints_are_unique_and_stable():
    registered = [registered_graph(g) for g in graph_definition_ids()]

    assert len({r.fingerprint for r in registered}) == len(registered)
    for graph_id, (name, build) in _GRAPHS.items():
        rebuilt = extract_definition(build(), graph_id, name)
        assert topology_fingerprint(rebuilt) == registered_graph(graph_id).fingerprint


@pytest.mark.backend_unit
def test_unknown_graph_definition_id_is_rejected():
    with pytest.raises(KeyError):
        registered_graph("not-a-graph@v1")


@pytest.mark.backend_unit
def test_models_reject_unknown_fields_and_bad_ids():
    definition = registered_graph("task-graph@v1").definition.model_dump(mode="json")

    with pytest.raises(ValidationError):
        GraphDefinition.model_validate({**definition, "extra": 1})
    with pytest.raises(ValidationError):
        GraphDefinition.model_validate({**definition, "graph_definition_id": "Task Graph"})
    with pytest.raises(ValidationError):
        TopologyEdge(source="a", target="b", kind="maybe")
    assert GraphDefinition.model_validate_json(json.dumps(definition)).model_dump(mode="json") == definition


@pytest.mark.backend_unit
def test_graph_instance_has_exactly_the_seven_contract_fields():
    instance = GraphInstance(
        graph_instance_id="gi-1",
        graph_definition_id="generic-domain@v1",
        parent_graph_instance_id=None,
        parent_node_run_id=None,
        namespace=[],
        started_sequence=1,
        ended_sequence=None,
    )

    assert set(instance.model_dump()) == {
        "graph_instance_id", "graph_definition_id", "parent_graph_instance_id",
        "parent_node_run_id", "namespace", "started_sequence", "ended_sequence",
    }
    with pytest.raises(ValidationError):
        GraphInstance.model_validate({**instance.model_dump(), "started_sequence": 0})
