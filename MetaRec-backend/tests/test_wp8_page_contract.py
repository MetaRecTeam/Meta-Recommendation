"""WP8 GraphTraceConfigV1 and GraphTraceV1 contract: shape, caps and page rules.

The brief's "Debug-run trace input" and "Pollable trace output" sections are
normative. The examples below are taken from them verbatim so a change to the
models has to change the brief too.
"""
import copy

import pytest
from pydantic import ValidationError

from langgraph_metarec.wp8_trace.models import (
    DEFAULT_RETENTION_DAYS,
    MAX_EVENTS,
    MAX_PAGE_EVENTS,
    MAX_RETENTION_DAYS,
    GraphTraceConfigV1,
    GraphTraceV1,
    contract_json_schema,
)

pytestmark = pytest.mark.backend_unit

# The example from the brief's "Debug-run trace input" section, verbatim.
SPEC_CONFIG = {
    "schema_version": "graph-trace-config/1.0",
    "enabled": True,
    "capture": {
        "graph_lifecycle": True,
        "node_lifecycle": True,
        "nested_graphs": True,
        "child_events": "tool_llm_metadata",
        "state_diff": "allowlisted_node_update",
    },
    "redaction_policy_id": "metarec-debug-safe-v1",
    "limits": {
        "max_events": 5000,
        "max_event_bytes": 16384,
        "max_trace_bytes": 2097152,
        "max_string_chars": 512,
        "max_collection_items": 50,
        "max_nesting_depth": 6,
    },
    "retention_days": 7,
    "correlation": {"fixture_id": "optional-safe-id", "case_id": "optional-safe-id"},
}

# The example from the brief's "Pollable trace output" section, verbatim.
SPEC_PAGE = {
    "schema_version": "graph-trace/1.0",
    "trace_id": "opaque-trace-id",
    "debug_run_id": "opaque-debug-run-id",
    "status": "running",
    "config_snapshot": {
        "redaction_policy_id": "metarec-debug-safe-v1",
        "capture": SPEC_CONFIG["capture"],
        "limits": SPEC_CONFIG["limits"],
        "retention_days": 7,
    },
    "topology": {
        "definitions": [
            {
                "graph_definition_id": "generic-domain@v1",
                "name": "Generic domain graph",
                "nodes": [{"node_id": "candidate_gather", "label": "Candidate gather", "kind": "node"}],
                "edges": [{"source": "__start__", "target": "candidate_gather", "kind": "normal", "label": None}],
            }
        ],
        "instances": [
            {
                "graph_instance_id": "opaque-instance-id",
                "graph_definition_id": "generic-domain@v1",
                "parent_graph_instance_id": None,
                "parent_node_run_id": None,
                "namespace": [],
                "started_sequence": 1,
                "ended_sequence": None,
            }
        ],
    },
    "events": [],
    "event_window": {"after_sequence": 0, "returned": 0, "next_after_sequence": 0, "has_more": False},
    "summary": {
        "started_at": "2026-08-20T10:30:00Z",
        "completed_at": None,
        "duration_ms": None,
        "visited_nodes": 0,
        "error_count": 0,
    },
    "redaction_summary": {"redacted_value_count": 0, "truncated_value_count": 0},
    "truncated": False,
    "dropped_event_count": 0,
    "created_at": "2026-08-20T10:30:00Z",
    "expires_at": "2026-08-27T10:30:00Z",
}


def _config(**overrides):
    payload = copy.deepcopy(SPEC_CONFIG)
    payload.update(overrides)
    return payload


def _page(**overrides):
    payload = copy.deepcopy(SPEC_PAGE)
    payload.update(overrides)
    return payload


# --- GraphTraceConfigV1 ------------------------------------------------------

def test_spec_config_example_validates():
    GraphTraceConfigV1.model_validate(SPEC_CONFIG)


def test_correlation_is_optional():
    payload = _config()
    payload.pop("correlation")
    GraphTraceConfigV1.model_validate(payload)


@pytest.mark.parametrize("field", ["schema_version", "enabled", "capture", "redaction_policy_id", "limits", "retention_days"])
def test_required_config_fields(field):
    payload = _config()
    payload.pop(field)
    with pytest.raises(ValidationError):
        GraphTraceConfigV1.model_validate(payload)


def test_unknown_top_level_field_rejected():
    with pytest.raises(ValidationError):
        GraphTraceConfigV1.model_validate(_config(unexpected="x"))


@pytest.mark.parametrize("key", ["graph_lifecycle", "node_lifecycle", "nested_graphs", "child_events", "state_diff"])
def test_all_five_capture_keys_required(key):
    payload = _config()
    payload["capture"].pop(key)
    with pytest.raises(ValidationError):
        GraphTraceConfigV1.model_validate(payload)


@pytest.mark.parametrize("days", [0, MAX_RETENTION_DAYS + 1])
def test_retention_outside_one_to_thirty_rejected(days):
    with pytest.raises(ValidationError):
        GraphTraceConfigV1.model_validate(_config(retention_days=days))


def test_requested_limit_above_hard_cap_rejected():
    payload = _config()
    payload["limits"]["max_events"] = MAX_EVENTS + 1
    with pytest.raises(ValidationError):
        GraphTraceConfigV1.model_validate(payload)


def test_lower_requested_limits_allowed():
    payload = _config()
    payload["limits"] = {k: 1 for k in payload["limits"]}
    assert GraphTraceConfigV1.model_validate(payload).limits.max_events == 1


def test_correlation_rejects_user_text():
    # Only [A-Za-z0-9._-]{1,64}; a sentence or an email must not pass.
    for bad in ["spicy sichuan in chinatown", "a@b.com", "x" * 65]:
        with pytest.raises(ValidationError):
            GraphTraceConfigV1.model_validate(_config(correlation={"fixture_id": bad, "case_id": None}))


# --- GraphTraceV1 ------------------------------------------------------------

def test_spec_page_example_validates():
    GraphTraceV1.model_validate(SPEC_PAGE)


@pytest.mark.parametrize("status", ["running", "completed", "failed", "cancelled", "incomplete"])
def test_every_trace_status_accepted(status):
    GraphTraceV1.model_validate(_page(status=status))


def test_unknown_trace_status_rejected():
    with pytest.raises(ValidationError):
        GraphTraceV1.model_validate(_page(status="finished"))


@pytest.mark.parametrize(
    "field",
    [
        "schema_version", "trace_id", "debug_run_id", "status", "config_snapshot",
        "topology", "events", "event_window", "summary", "redaction_summary",
        "truncated", "dropped_event_count", "created_at", "expires_at",
    ],
)
def test_every_page_field_required(field):
    payload = _page()
    payload.pop(field)
    with pytest.raises(ValidationError):
        GraphTraceV1.model_validate(payload)


def test_nullable_summary_fields_while_running():
    page = GraphTraceV1.model_validate(SPEC_PAGE)
    assert page.summary.completed_at is None
    assert page.summary.duration_ms is None


def test_returned_must_match_event_count():
    payload = _page()
    payload["event_window"]["returned"] = 3
    with pytest.raises(ValidationError, match="returned"):
        GraphTraceV1.model_validate(payload)


def test_cursor_may_not_move_backwards():
    payload = _page()
    payload["event_window"] = {"after_sequence": 30, "returned": 0, "next_after_sequence": 10, "has_more": False}
    with pytest.raises(ValidationError, match="backwards"):
        GraphTraceV1.model_validate(payload)


def test_expiry_must_follow_creation():
    payload = _page(expires_at=SPEC_PAGE["created_at"])
    with pytest.raises(ValidationError, match="expires_at"):
        GraphTraceV1.model_validate(payload)


def test_default_retention_is_seven_days():
    assert DEFAULT_RETENTION_DAYS == 7


def test_page_cap_is_five_hundred():
    assert MAX_PAGE_EVENTS == 500


def test_page_schema_exposes_event_and_topology_models():
    schema = contract_json_schema(GraphTraceV1)
    defs = schema.get("$defs", {})
    for model in ("GraphRunEventV1", "GraphDefinition", "GraphInstance"):
        assert model in defs, f"{model} must reach consumers through the page schema"
