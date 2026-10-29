"""WP8 golden event lists (from the real D0 recordings): valid, reproducible, honest."""
import json
import sys
from collections import Counter
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from langgraph_metarec.wp8_trace.models import GraphRunEventV1
from langgraph_metarec.wp8_trace.topology import registered_graph

BACKEND_DIR = Path(__file__).resolve().parents[1]
GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "wp8" / "golden"
EVENT_SCHEMA = json.loads((BACKEND_DIR.parent / "contracts" / "wp8" / "graph-run-event-v1.schema.json").read_text())
CASES = {p.stem: json.loads(p.read_text()) for p in sorted(GOLDEN_DIR.glob("*.json"))}
ROUTERS = {"_route_after_collect_confirm", "route_after_classification", "route_after_retry"}


def _terminal(events):
    return [e for e in events if e["event_type"].endswith("_end") or e["event_type"] == "error"]


@pytest.mark.backend_unit
def test_every_planned_case_exists():
    assert set(CASES) == {
        "query_turn_routing", "confirm_turn_task", "itinerary_anchor", "repeated_domain_graphs",
        "caught_failure", "uncaught_failure", "cancellation", "timeout", "truncation", "incomplete",
    }


@pytest.mark.backend_unit
def test_fixtures_are_reproducible_from_the_d0_recordings():
    sys.path.insert(0, str(BACKEND_DIR / "scripts"))
    from wp8_golden_events import build, render

    built = build()
    assert set(built) == set(CASES)
    for name, case in built.items():
        assert (GOLDEN_DIR / f"{name}.json").read_text() == render(case), (
            f"{name}.json is stale: run scripts/wp8_golden_events.py"
        )


@pytest.mark.backend_unit
@pytest.mark.parametrize("name", list(CASES))
def test_events_satisfy_model_and_checked_in_schema(name):
    validator = Draft202012Validator(EVENT_SCHEMA)
    for event in CASES[name]["events"]:
        GraphRunEventV1.model_validate(event)
        assert [e.message for e in validator.iter_errors(event)] == []


@pytest.mark.backend_unit
@pytest.mark.parametrize("name", list(CASES))
def test_trace_structure_is_sound(name):
    events = CASES[name]["events"]
    lifecycle = [e for e in events if e["event_type"] != "trace_truncated"]

    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert len({e["trace_id"] for e in events}) == 1

    roots = [e for e in lifecycle if e["event_type"] == "graph_start" and e["parent_run_id"] is None]
    assert len(roots) == 1

    started = {}
    for e in lifecycle:
        if e["event_type"].endswith("_start"):
            assert e["run_id"] not in started, "one start per run"
            if e["parent_run_id"] is not None:
                assert e["parent_run_id"] in started, "a parent starts before its child"
            started[e["run_id"]] = e
        else:
            assert e["run_id"] in started, "every end or error closes a started run"
            assert e["parent_run_id"] == started[e["run_id"]]["parent_run_id"]
    assert all(n == 1 for n in Counter(e["run_id"] for e in _terminal(lifecycle)).values()), "at most one terminal event"

    for e in lifecycle:
        assert e["node_id"] not in ROUTERS, "edge routers are not node visits"
        if e["node_id"] is not None:
            nodes = {n.node_id for n in registered_graph(e["graph_definition_id"]).definition.nodes}
            assert e["node_id"] in nodes


@pytest.mark.backend_unit
@pytest.mark.parametrize("name", list(CASES))
def test_manifest_matches_the_events(name):
    manifest, events = CASES[name]["manifest"], CASES[name]["events"]
    closed = {e["run_id"] for e in _terminal(events)}

    assert manifest["lifecycle_counts"] == dict(sorted(Counter(e["event_type"] for e in events).items()))
    assert manifest["unmatched_start_run_ids"] == [
        e["run_id"] for e in events if e["event_type"].endswith("_start") and e["run_id"] not in closed
    ]
    assert manifest["error_count"] == sum(1 for e in events if e["event_type"] == "error")
    assert manifest["truncated"] == any(e["event_type"] == "trace_truncated" for e in events)


@pytest.mark.backend_unit
def test_task_outlives_the_request_graph():
    events = CASES["confirm_turn_task"]["events"]
    root_end = next(e["sequence"] for e in events if e["event_type"] == "graph_end" and e["parent_run_id"] is None)
    task_end = next(e["sequence"] for e in events
                    if e["event_type"] == "graph_end" and e["graph_definition_id"] == "task-graph@v1")

    assert task_end > root_end


@pytest.mark.backend_unit
def test_failure_and_cancellation_close_runs_with_error_events():
    def errors(name):
        return [(e["graph_definition_id"], e["node_id"], e["status"]) for e in CASES[name]["events"]
                if e["event_type"] == "error"]

    assert errors("caught_failure") == [
        ("generic-domain@v1", "candidate_gather", "error"), ("generic-domain@v1", None, "error")]
    assert {s for *_, s in errors("cancellation")} == {"cancelled"}
    assert [n for _, n, _ in errors("uncaught_failure")] == ["domain_classification", None, "routing", None]
    assert {s for *_, s in errors("timeout")} == {"cancelled"}


@pytest.mark.backend_unit
def test_repeated_graphs_are_two_instances_under_one_node_run():
    instances = [i for i in CASES["repeated_domain_graphs"]["manifest"]["graph_instances"]
                 if i["graph_definition_id"] == "generic-domain@v1"]

    assert len(instances) == 2
    assert len({i["graph_instance_id"] for i in instances}) == 2
    assert len({i["parent_node_run_id"] for i in instances}) == 1


@pytest.mark.backend_unit
def test_synthetic_cases_are_labelled_and_honest():
    truncation, incomplete = CASES["truncation"], CASES["incomplete"]

    assert truncation["manifest"]["source"]["synthetic"] and incomplete["manifest"]["source"]["synthetic"]
    assert [e["event_type"] for e in truncation["events"]].count("trace_truncated") == 1
    assert truncation["events"][-1]["event_type"] == "trace_truncated"
    assert truncation["manifest"]["dropped_event_count"] > 0
    assert incomplete["manifest"]["expected_trace_status"] == "incomplete"
    assert incomplete["manifest"]["unmatched_start_run_ids"]
