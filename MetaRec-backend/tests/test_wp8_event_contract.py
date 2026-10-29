"""WP8 GraphRunEventV1 contract: shape, hard caps and the required/null matrix."""
import copy
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from langgraph_metarec.wp8_trace.models import GraphRunEventV1, MAX_COLLECTION_ITEMS, MAX_NESTING_DEPTH, MAX_STRING_CHARS

BACKEND_DIR = Path(__file__).resolve().parents[1]
EVENT_SCHEMA = json.loads((BACKEND_DIR.parent / "contracts" / "wp8" / "graph-run-event-v1.schema.json").read_text())
# JSON cannot carry NaN/Infinity, so only the Python model can reject them.
PYTHON_ONLY = {"non-finite number"}
VALID = json.loads((Path(__file__).resolve().parent / "fixtures" / "wp8" / "events" / "valid_events.json").read_text())

# The example event from the WP8 brief's "Atomic event output" section, verbatim.
SPEC_EXAMPLE = {
    "schema_version": "graph-run-event/1.0",
    "event_id": "opaque-event-id",
    "trace_id": "opaque-trace-id",
    "debug_run_id": "opaque-debug-run-id",
    "sequence": 17,
    "timestamp_utc": "2026-08-20T10:30:00.123Z",
    "monotonic_offset_ms": 84.2,
    "event_type": "node_end",
    "status": "completed",
    "graph_definition_id": "generic-domain@v1",
    "graph_instance_id": "opaque-graph-instance-id",
    "parent_graph_instance_id": "opaque-parent-instance-id",
    "node_id": "candidate_gather",
    "run_id": "opaque-callback-run-id",
    "parent_run_id": "opaque-parent-run-id",
    "runnable_type": "graph_node",
    "duration_ms": 42.8,
    "input_summary": {"keys": ["domain", "preferences"], "bytes": 220},
    "output_summary": {"keys": ["executions"], "bytes": 480},
    "state_diff": {
        "basis": "node_input_to_update",
        "added": {"candidate_count": 12},
        "changed": {},
        "removed": [],
        "omitted": [{"path": "$.raw", "reason": "not_allowlisted"}],
    },
    "metadata": {"domain": "movie", "tool": None, "model": None},
    "error": None,
    "redaction": {
        "policy_id": "metarec-debug-safe-v1",
        "redacted_paths": [],
        "truncated_paths": ["$.output_summary"],
        "stored_bytes": 1160,
    },
}


def _with(name, **changes):
    event = copy.deepcopy(VALID[name])
    event.update(changes)
    return event


def _nested(value, depth):
    for _ in range(depth):
        value = {"k": value}
    return value


@pytest.mark.backend_unit
def test_spec_example_is_valid():
    GraphRunEventV1.model_validate(SPEC_EXAMPLE)


@pytest.mark.backend_unit
@pytest.mark.parametrize("name", list(VALID))
def test_valid_fixture_round_trips_with_every_key_present(name):
    event = GraphRunEventV1.model_validate(VALID[name])
    dumped = json.loads(event.model_dump_json())

    assert set(dumped) == set(GraphRunEventV1.model_fields)
    assert GraphRunEventV1.model_validate(dumped) == event


@pytest.mark.backend_unit
def test_fixtures_cover_every_event_type_family():
    assert {e["event_type"] for e in VALID.values()} >= {
        "graph_start", "graph_end", "node_start", "node_end", "tool_end",
        "llm_start", "policy_end", "error", "trace_truncated",
    }


INVALID = [
    # shape
    ("unknown top-level field", lambda: _with("node_start", extra_field=1)),
    ("missing key, not null", lambda: {k: v for k, v in VALID["node_start"].items() if k != "duration_ms"}),
    ("bad event_type", lambda: _with("node_start", event_type="node_visit")),
    ("bad status", lambda: _with("node_start", status="done")),
    ("sequence starts at 1", lambda: _with("node_start", sequence=0)),
    ("non-UTC timestamp", lambda: _with("node_start", timestamp_utc="2026-08-20T18:30:00+08:00")),
    ("negative duration", lambda: _with("graph_end", duration_ms=-1.0)),
    ("user text in an id", lambda: _with("node_start", run_id="find me a hotel")),
    ("bad graph_definition_id", lambda: _with("node_start", graph_definition_id="Request Orchestrator")),
    ("redaction is always present", lambda: _with("node_start", redaction=None)),
    # required/null matrix
    ("runnable_type must fit the event", lambda: _with("node_start", runnable_type="graph")),
    ("graph lifecycle has no node_id", lambda: _with("root_graph_start", node_id="intention")),
    ("node needs its graph run parent", lambda: _with("node_start", parent_run_id=None)),
    ("node needs node_id", lambda: _with("node_start", node_id=None)),
    ("lifecycle needs run_id", lambda: _with("node_start", run_id=None)),
    ("lifecycle needs graph_instance_id", lambda: _with("node_start", graph_instance_id=None)),
    ("instance needs its definition", lambda: _with("node_start", graph_definition_id=None)),
    ("tool child span needs the triggering node run", lambda: _with("tool_end_with_health", parent_run_id=None)),
    ("tool child span needs the triggering node", lambda: _with("tool_end_with_health", node_id=None)),
    ("nested graph links to its invoking node run", lambda: _with("nested_graph_start", parent_run_id=None)),
    ("error event carries an error", lambda: _with("node_error_cancelled", error=None)),
    ("error event status", lambda: _with("node_error_cancelled", status="completed")),
    ("node-level error needs node_id", lambda: _with("node_error_cancelled", node_id=None)),
    ("trace_truncated has no run", lambda: _with("trace_truncated", run_id="r0")),
    ("trace_truncated is a trace_system event", lambda: _with("trace_truncated", runnable_type="graph")),
    ("starts have no duration", lambda: _with("node_start", duration_ms=1.0)),
    ("starts are running", lambda: _with("node_start", status="completed")),
    ("ends are not running", lambda: _with("graph_end", status="running")),
    ("state_diff only on node_end", lambda: _with("node_start", state_diff=VALID["node_end_with_state_diff"]["state_diff"])),
    ("llm_attempt only on WP5 adapter events", lambda: _with("llm_attempt_start", runnable_type="llm")),
    ("tool_scope only on WP7 adapter events", lambda: _with("tool_scope_policy_end", runnable_type="policy")),
    ("attempt is positive", lambda: _with("llm_attempt_start", metadata={"attempt": 0})),
    ("metadata keys are fixed", lambda: _with("node_start", metadata={"user_email": "a@b.c"})),
    # safe-value grammar and hard caps
    ("string over the cap", lambda: _with("node_end_with_state_diff", state_diff={**VALID["node_end_with_state_diff"]["state_diff"], "added": {"x": "a" * (MAX_STRING_CHARS + 1)}})),
    ("collection over the cap", lambda: _with("node_end_with_state_diff", state_diff={**VALID["node_end_with_state_diff"]["state_diff"], "added": {"x": list(range(MAX_COLLECTION_ITEMS + 1))}})),
    ("nesting over the cap", lambda: _with("node_end_with_state_diff", state_diff={**VALID["node_end_with_state_diff"]["state_diff"], "added": {"x": _nested(1, MAX_NESTING_DEPTH + 1)}})),
    ("non-finite number", lambda: _with("node_end_with_state_diff", state_diff={**VALID["node_end_with_state_diff"]["state_diff"], "added": {"x": float("nan")}})),
    ("too many summary keys", lambda: _with("node_end_with_state_diff", input_summary={"keys": [str(i) for i in range(MAX_COLLECTION_ITEMS + 1)], "bytes": 1})),
    ("unknown summary field", lambda: _with("node_end_with_state_diff", input_summary={"keys": [], "bytes": 1, "raw": "prompt"})),
    ("unknown error field", lambda: _with("root_graph_error", error={**VALID["root_graph_error"]["error"], "traceback": "..."})),
]


@pytest.mark.backend_unit
@pytest.mark.parametrize("label,build", INVALID, ids=[label for label, _ in INVALID])
def test_invalid_event_is_rejected(label, build):
    with pytest.raises(ValidationError):
        GraphRunEventV1.model_validate(build())


@pytest.mark.backend_unit
def test_nesting_at_the_cap_is_allowed():
    diff = {**VALID["node_end_with_state_diff"]["state_diff"], "added": {"x": _nested(1, MAX_NESTING_DEPTH)}}

    GraphRunEventV1.model_validate(_with("node_end_with_state_diff", state_diff=diff))


@pytest.mark.backend_unit
def test_checked_in_json_schemas_match_the_models():
    sys.path.insert(0, str(BACKEND_DIR / "scripts"))
    from wp8_export_schemas import SCHEMA_DIR, SCHEMAS, render

    for stem in SCHEMAS:
        path = SCHEMA_DIR / f"{stem}.schema.json"
        assert path.read_text() == render(stem), f"{path.name} is stale: run scripts/wp8_export_schemas.py"
    assert sorted(p.name for p in SCHEMA_DIR.glob("*.schema.json")) == sorted(f"{s}.schema.json" for s in SCHEMAS)


@pytest.mark.backend_unit
def test_event_schema_rejects_unknown_fields_and_requires_every_key():
    schema = GraphRunEventV1.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(GraphRunEventV1.model_fields)


@pytest.mark.backend_unit
def test_checked_in_event_schema_is_valid_2020_12():
    Draft202012Validator.check_schema(EVENT_SCHEMA)


@pytest.mark.backend_unit
@pytest.mark.parametrize("name", [*VALID, "spec_example"])
def test_valid_event_passes_the_checked_in_schema(name):
    event = SPEC_EXAMPLE if name == "spec_example" else VALID[name]

    assert [e.message for e in Draft202012Validator(EVENT_SCHEMA).iter_errors(event)] == []


@pytest.mark.backend_unit
@pytest.mark.parametrize(
    "label,build",
    [case for case in INVALID if case[0] not in PYTHON_ONLY],
    ids=[label for label, _ in INVALID if label not in PYTHON_ONLY],
)
def test_invalid_event_fails_the_checked_in_schema(label, build):
    # A consumer validating with the schema alone rejects what Python rejects.
    assert not Draft202012Validator(EVENT_SCHEMA).is_valid(build())
