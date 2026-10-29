"""Build the WP8 golden event lists from the real D0 probe recordings.

Each case is one request turn recorded by scripts/wp8_parentage_probe.py
(docs/wp8/parentage-baseline/data/), converted into GraphRunEventV1 events the
way the collector is meant to: edge routers dropped, graphs named from the
topology registry, a failed or cancelled run closed by one `error` event on
its own run_id. Two cases (truncation, crash) are derived from a real turn and
marked synthetic. Values were never recorded, so summaries, diffs and
metadata are null and redaction is empty; Member B's projector fills those.

Usage (from MetaRec-backend/):
    ../.venv/bin/python scripts/wp8_golden_events.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "tests"))

from langgraph_metarec.wp8_trace.models import GraphRunEventV1  # noqa: E402
from langgraph_metarec.wp8_trace.topology import graph_definition_ids, registered_graph  # noqa: E402
from wp8_probe_support import GRAPH_NAMES, build_runs, identify  # noqa: E402

D0_DATA = BACKEND_DIR / "docs" / "wp8" / "parentage-baseline" / "data"
GOLDEN_DIR = BACKEND_DIR / "tests" / "fixtures" / "wp8" / "golden"
BASE_TIME = datetime(2026, 10, 4, tzinfo=timezone.utc)
POLICY_ID = "metarec-debug-safe-v1"

DEFINITION_BY_GRAPH = {
    "request_orchestrator": "request-orchestrator@v1",
    "routing_graph": "routing-graph@v1",
    "task_graph": "task-graph@v1",
    "generic_graph": "generic-domain@v1",
    "restaurant_graph": "restaurant-domain@v1",
}
ROUTERS = frozenset().union(*(registered_graph(g).routers for g in graph_definition_ids()))

# case -> (D0 experiment, 1-based root turn, expected trace status, description)
CASES = {
    "query_turn_routing": ("E3_service_hotel_chain", 1, "completed",
                           "Hotel query turn: routing_graph nested under the orchestrator's routing node."),
    "confirm_turn_task": ("E3_service_hotel_chain", 2, "completed",
                          "Confirm turn: task_graph under domain_dispatch outlives the request graph; "
                          "generic_graph under domain_graph_dispatch."),
    "itinerary_anchor": ("E4_itinerary_anchor", 1, "completed",
                         "Itinerary turn: generic_graph nested under domain_dispatch before any task exists."),
    "repeated_domain_graphs": ("E11_multi_domain_siblings", 2, "completed",
                               "Multi-domain confirm turn: two generic_graph instances under one domain_graph_dispatch run."),
    "caught_failure": ("E7_domain_failure_caught", 2, "completed",
                       "generic_graph fails, the service catches it, ancestors and the task report success."),
    "uncaught_failure": ("E8_routing_failure_uncaught", 1, "failed",
                         "routing_graph fails and the error closes every enclosing run, innermost first."),
    "cancellation": ("E9_background_cancel", 2, "cancelled",
                     "Background task cancelled mid tool call; the request graph had already completed."),
    "timeout": ("E10_request_timeout", 1, "cancelled",
                "The request times out while the intention node waits on the LLM."),
}
TRUNCATE_AFTER = 11  # ordinary events kept before the trace_truncated marker
CRASH_AFTER_NODE = ("generic-domain@v1", "candidate_gather")


def _descendants(runs: Dict[str, Dict[str, Any]], root_id: str) -> set:
    keep, frontier = {root_id}, [root_id]
    while frontier:
        parent = frontier.pop()
        for run in runs.values():
            if run["parent_run_id"] == parent and run["run_id"] not in keep:
                keep.add(run["run_id"])
                frontier.append(run["run_id"])
    return keep


def convert(experiment: str, root_index: int, case: str) -> List[Dict[str, Any]]:
    recorded = json.loads((D0_DATA / f"{experiment}.events.json").read_text())
    runs = build_runs(recorded)
    roots = sorted((r for r in runs.values() if r["parent_run_id"] is None), key=lambda r: r["start_ms"])
    root = roots[root_index - 1]
    tree = _descendants(runs, root["run_id"])
    t0 = root["start_ms"]

    run_ids: Dict[str, str] = {}
    instance_of: Dict[str, str] = {}      # graph run -> graph_instance_id
    definition_of: Dict[str, str] = {}    # graph run -> graph_definition_id
    graph_of_node: Dict[str, str] = {}    # node run -> enclosing graph run
    events: List[Dict[str, Any]] = []

    def rid(raw: Optional[str]) -> Optional[str]:
        if raw is None:
            return None
        return run_ids.setdefault(raw, f"run-{len(run_ids) + 1:03d}")

    for recorded_event in recorded:
        raw = recorded_event["run_id"]
        if raw not in tree:
            continue
        run = runs[raw]
        if run["name"] in ROUTERS:
            continue
        is_graph = run["name"] in GRAPH_NAMES
        starting = recorded_event["event"] == "chain_start"
        failed = recorded_event["event"] == "chain_error"

        if is_graph:
            if starting:
                definition_of[raw] = DEFINITION_BY_GRAPH[identify(run, runs)]
                instance_of[raw] = f"gi-{len(instance_of) + 1:02d}"
            graph_run, node_id, runnable = raw, None, "graph"
            parent_node = run["parent_run_id"]
            parent_instance = instance_of[graph_of_node[parent_node]] if parent_node else None
        else:
            graph_run = run["parent_run_id"]
            graph_of_node[raw] = graph_run
            node_id, runnable = run["name"], "graph_node"
            parent_instance = None
            known = {n.node_id for n in registered_graph(definition_of[graph_run]).definition.nodes}
            assert node_id in known, f"{case}: {node_id} is not a node of {definition_of[graph_run]}"

        t = recorded_event["t_ms"]
        kind = ("graph" if is_graph else "node") + ("_start" if starting else "_end")
        status, error = ("running" if starting else "completed"), None
        if failed:
            cancelled = recorded_event.get("error_class") == "CancelledError"
            kind, status = "error", ("cancelled" if cancelled else "error")
            error = {
                "code": "cancelled" if cancelled else "execution_failed",
                "error_class": recorded_event.get("error_class") or "Exception",
                "safe_message": "Run was cancelled" if cancelled else "Run failed",
                "retryable": False,
            }
        events.append({
            "schema_version": "graph-run-event/1.0",
            "event_id": f"ev-{len(events) + 1:04d}",
            "trace_id": f"trace-{case}",
            "debug_run_id": f"debug-{case}",
            "sequence": len(events) + 1,
            "timestamp_utc": (BASE_TIME + timedelta(milliseconds=t)).isoformat().replace("+00:00", "Z"),
            "monotonic_offset_ms": round(t - t0, 3),
            "event_type": kind,
            "status": status,
            "graph_definition_id": definition_of[graph_run],
            "graph_instance_id": instance_of[graph_run],
            "parent_graph_instance_id": parent_instance,
            "node_id": node_id,
            "run_id": rid(raw),
            "parent_run_id": rid(run["parent_run_id"]),
            "runnable_type": runnable,
            "duration_ms": None if starting else round(t - run["start_ms"], 3),
            "input_summary": None,
            "output_summary": None,
            "state_diff": None,
            "metadata": None,
            "error": error,
            "redaction": {"policy_id": POLICY_ID, "redacted_paths": [], "truncated_paths": [], "stored_bytes": 0},
        })
    return events


def truncate(events: List[Dict[str, Any]], case: str) -> List[Dict[str, Any]]:
    """A trace whose cap leaves room for TRUNCATE_AFTER events plus one marker."""
    kept = [dict(e, trace_id=f"trace-{case}", debug_run_id=f"debug-{case}") for e in events[:TRUNCATE_AFTER]]
    root = events[0]
    kept.append({
        **{k: None for k in events[0]},
        "schema_version": "graph-run-event/1.0",
        "event_id": f"ev-{len(kept) + 1:04d}",
        "trace_id": f"trace-{case}",
        "debug_run_id": f"debug-{case}",
        "sequence": len(kept) + 1,
        "timestamp_utc": kept[-1]["timestamp_utc"],
        "monotonic_offset_ms": kept[-1]["monotonic_offset_ms"],
        "event_type": "trace_truncated",
        "status": "incomplete",
        "graph_definition_id": root["graph_definition_id"],
        "graph_instance_id": root["graph_instance_id"],
        "runnable_type": "trace_system",
        "redaction": {"policy_id": POLICY_ID, "redacted_paths": [], "truncated_paths": [], "stored_bytes": 0},
    })
    return kept


def crash(events: List[Dict[str, Any]], case: str) -> List[Dict[str, Any]]:
    """The process dies right after the first CRASH_AFTER_NODE starts."""
    definition, node = CRASH_AFTER_NODE
    cut = next(i for i, e in enumerate(events)
               if e["event_type"] == "node_start" and e["graph_definition_id"] == definition and e["node_id"] == node)
    return [dict(e, trace_id=f"trace-{case}", debug_run_id=f"debug-{case}") for e in events[:cut + 1]]


def manifest(case: str, events: List[Dict[str, Any]], *, source: Dict[str, Any], status: str,
             description: str, dropped: int = 0) -> Dict[str, Any]:
    terminal = {e["run_id"] for e in events if e["event_type"].endswith("_end") or e["event_type"] == "error"}
    starts = [e for e in events if e["event_type"].endswith("_start")]
    instances = []
    for e in starts:
        if e["event_type"] == "graph_start":
            instances.append({
                "graph_instance_id": e["graph_instance_id"],
                "graph_definition_id": e["graph_definition_id"],
                "parent_graph_instance_id": e["parent_graph_instance_id"],
                "parent_node_run_id": e["parent_run_id"],
                "closed": e["run_id"] in terminal,
            })
    visited: Dict[str, List[str]] = {}
    for e in starts:
        if e["event_type"] == "node_start":
            visited.setdefault(e["graph_instance_id"], []).append(e["node_id"])
    return {
        "case": case,
        "description": description,
        "source": source,
        "expected_trace_status": status,
        "truncated": any(e["event_type"] == "trace_truncated" for e in events),
        "dropped_event_count": dropped,
        "graph_instances": instances,
        "visited_nodes_by_instance": visited,
        "lifecycle_counts": dict(sorted(Counter(e["event_type"] for e in events).items())),
        "unmatched_start_run_ids": [e["run_id"] for e in starts if e["run_id"] not in terminal],
        "error_count": sum(1 for e in events if e["event_type"] == "error"),
    }


def build() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for case, (experiment, root_index, status, description) in CASES.items():
        events = convert(experiment, root_index, case)
        source = {"experiment": experiment, "root_turn": root_index, "synthetic": None}
        out[case] = {"manifest": manifest(case, events, source=source, status=status, description=description),
                     "events": events}

    base_events = out["confirm_turn_task"]["events"]
    base = out["confirm_turn_task"]["manifest"]["source"]
    events = truncate(base_events, "truncation")
    out["truncation"] = {
        "manifest": manifest(
            "truncation", events, status="completed",
            source={**base, "synthetic": f"confirm_turn_task cut at a {TRUNCATE_AFTER}-event cap plus one trace_truncated marker"},
            description="The event cap is reached: one trace_truncated marker, the rest dropped and counted.",
            dropped=len(base_events) - TRUNCATE_AFTER),
        "events": events,
    }
    events = crash(base_events, "incomplete")
    out["incomplete"] = {
        "manifest": manifest(
            "incomplete", events, status="incomplete",
            source={**base, "synthetic": "confirm_turn_task cut right after generic_graph's candidate_gather starts (process crash)"},
            description="The process dies mid-run; open runs stay unmatched and recovery marks the trace incomplete."),
        "events": events,
    }

    for case in out.values():
        for event in case["events"]:
            GraphRunEventV1.model_validate(event)
    return out


def render(case: Dict[str, Any]) -> str:
    return json.dumps(case, indent=2) + "\n"


def main() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for stale in GOLDEN_DIR.glob("*.json"):
        stale.unlink()
    for name, case in build().items():
        path = GOLDEN_DIR / f"{name}.json"
        path.write_text(render(case))
        m = case["manifest"]
        print(f"wrote {path.relative_to(BACKEND_DIR)}  events={len(case['events'])} "
              f"status={m['expected_trace_status']} unmatched={len(m['unmatched_start_run_ids'])}")


if __name__ == "__main__":
    main()
