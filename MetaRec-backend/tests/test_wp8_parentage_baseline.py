"""WP8 D0 baseline: callback parentage of the current graphs, observed at runtime.

These characterise today's behaviour with no WP8 code present. When WP8's
explicit propagation lands, test_task_handoff_parent_depends_on_inherited_context
is expected to change: a fresh-context hand-off must then still link.
"""
import asyncio

import pytest

from wp8_probe_support import (
    CallCounter,
    Gate,
    _hotel_chain,
    analyse,
    e3_service_hotel_chain,
    e4_itinerary_anchor,
    e5_concurrent_chains,
    e6a_handoff_inherited_context,
    e6b_handoff_fresh_context,
    e7_domain_failure_caught,
    e8_routing_failure_uncaught,
    e9_background_cancel,
    e10_request_timeout,
    e11_multi_domain_siblings,
    record,
)


async def _observe(experiment):
    events, outcome = await record(experiment)
    report = analyse(events, outcome)
    assert report["integrity"]["dangling_parents"] == 0
    assert report["integrity"]["cycles"] == 0
    assert report["integrity"]["unmatched_starts"] == 0
    return report


def _graphs(report, name):
    return [g for g in report["graphs"] if g["graph"] == name]


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_nested_routing_graph_has_routing_node_parent():
    report = await _observe(e3_service_hotel_chain)

    (routing,) = _graphs(report, "routing_graph")
    assert routing["parent_name"] == "routing"
    assert routing["parent_graph"] == "request_orchestrator"
    assert routing["ends_after_root"] is False


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_background_task_graph_keeps_parent_but_outlives_request_graph():
    report = await _observe(e3_service_hotel_chain)

    (task,) = _graphs(report, "task_graph")
    assert task["parent_name"] == "domain_dispatch"
    assert task["parent_graph"] == "request_orchestrator"
    assert task["parent_ended_before_child"] is True
    assert task["ends_after_root"] is True

    (generic,) = _graphs(report, "generic_graph")
    assert generic["parent_name"] == "domain_graph_dispatch"
    assert generic["parent_graph"] == "task_graph"
    assert report["outcome"]["task_status"] == "completed"


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_itinerary_anchor_nests_generic_graph_under_domain_dispatch():
    report = await _observe(e4_itinerary_anchor)

    (generic,) = _graphs(report, "generic_graph")
    assert generic["parent_name"] == "domain_dispatch"
    assert generic["parent_graph"] == "request_orchestrator"
    assert _graphs(report, "task_graph") == []


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_concurrent_requests_keep_their_own_parents():
    report = await _observe(e5_concurrent_chains)

    assert report["concurrency"]["task_graphs_checked"] == 10
    assert report["concurrency"]["cross_request_leaks"] == []
    assert all(t["parent_name"] == "domain_dispatch" for t in _graphs(report, "task_graph"))


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_task_handoff_parent_depends_on_inherited_context():
    inherited = await _observe(e6a_handoff_inherited_context)
    fresh = await _observe(e6b_handoff_fresh_context)

    (linked,) = _graphs(inherited, "task_graph")
    assert linked["parent_name"] == "handoff_node"
    (orphan,) = _graphs(fresh, "task_graph")
    assert orphan["parent_run_id"] is None


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_failure_inside_domain_graph_is_contained_by_service():
    report = await _observe(e7_domain_failure_caught)

    assert [e["run"] for e in report["errors"]] == ["generic_graph", "candidate_gather"]
    assert {e["error_class"] for e in report["errors"]} == {"RuntimeError"}
    (task,) = _graphs(report, "task_graph")
    assert task["status"] == "completed"
    assert report["outcome"]["task_status"] == "completed"
    assert report["outcome"]["item_titles"] == []


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_uncaught_routing_failure_errors_every_enclosing_run():
    report = await _observe(e8_routing_failure_uncaught)

    assert report["outcome"]["raised"] == "RuntimeError"
    assert [e["run"] for e in report["errors"]] == [
        "request_orchestrator", "routing", "routing_graph", "domain_classification",
    ]


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_background_cancellation_closes_runs_with_cancelled_error():
    report = await _observe(e9_background_cancel)

    assert report["outcome"]["background_await"] == "CancelledError"
    assert report["outcome"]["task_status"] == "cancelled"
    assert [e["run"] for e in report["errors"]] == [
        "task_graph", "domain_graph_dispatch", "generic_graph", "candidate_gather",
    ]
    assert {e["error_class"] for e in report["errors"]} == {"CancelledError"}
    assert all(g["status"] == "completed" for g in _graphs(report, "request_orchestrator"))


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_request_timeout_closes_root_with_cancelled_error():
    report = await _observe(e10_request_timeout)

    assert report["outcome"]["raised"] == "TimeoutError"
    assert report["errors"] == [
        {"run": "request_orchestrator", "error_class": "CancelledError"},
        {"run": "intention", "error_class": "CancelledError"},
    ]


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_multi_domain_runs_sibling_domain_graphs_under_one_node():
    report = await _observe(e11_multi_domain_siblings)

    generics = _graphs(report, "generic_graph")
    assert len(generics) == 2
    assert {g["parent_name"] for g in generics} == {"domain_graph_dispatch"}
    assert len({g["parent_run_id"] for g in generics}) == 1
    assert generics[0]["end_ms"] <= generics[1]["start_ms"]


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_callback_data_gaps_wp8_must_fill():
    report = await _observe(e3_service_hotel_chain)

    assert report["graph_root_run_names"] == ["LangGraph"]
    assert report["outcome"]["tool_adapter_calls"] > 0
    assert report["tool_or_llm_events"] == 0
    (task,) = _graphs(report, "task_graph")
    assert task["thread_id_seen"] == "u-probe-hotel:c-u-probe-hotel:branch-main"


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_listener_does_not_change_business_behaviour():
    _, traced = await record(lambda: _hotel_chain("u-exact-on", CallCounter(), Gate()))
    untraced = await _hotel_chain("u-exact-off", CallCounter(), Gate())

    assert traced["tool_adapter_calls"] == untraced["tool_adapter_calls"]
    assert traced["item_titles"] == untraced["item_titles"]
    assert traced["task_status"] == untraced["task_status"] == "completed"
