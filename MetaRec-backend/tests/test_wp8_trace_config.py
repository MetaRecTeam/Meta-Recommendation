"""B1: the server flag defaults off, and a capture request is validated once.

The flag tests matter more than they look: invariant 5 of the brief is that
with tracing off, nothing is captured and no existing response changes. That
holds only if "absent" reads as off.
"""
import pytest
from pydantic import ValidationError

from langgraph_metarec.wp8_trace.config import (
    DEFAULT_POLICY_ID,
    GRAPH_TRACE_FLAG,
    REGISTERED_POLICY_IDS,
    TraceConfigError,
    default_trace_config,
    effective_limits,
    graph_trace_enabled,
    retention_days,
    validate_trace_config,
)
from langgraph_metarec.wp8_trace.models import (
    MAX_EVENTS,
    MAX_RETENTION_DAYS,
    GraphTraceConfigV1,
)

pytestmark = pytest.mark.backend_unit


# --- the server flag ---------------------------------------------------------

def test_flag_absent_is_off():
    assert graph_trace_enabled({}) is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on", "On"])
def test_flag_truthy_spellings(raw):
    assert graph_trace_enabled({GRAPH_TRACE_FLAG: raw}) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "maybe", "2"])
def test_flag_anything_else_is_off(raw):
    assert graph_trace_enabled({GRAPH_TRACE_FLAG: raw}) is False


def test_flag_reads_real_environment(monkeypatch):
    monkeypatch.delenv(GRAPH_TRACE_FLAG, raising=False)
    assert graph_trace_enabled() is False
    monkeypatch.setenv(GRAPH_TRACE_FLAG, "true")
    assert graph_trace_enabled() is True


# --- the default config ------------------------------------------------------

def test_default_config_validates_and_is_full_capture():
    config = default_trace_config()
    assert isinstance(config, GraphTraceConfigV1)
    assert config.enabled is True
    assert config.capture.graph_lifecycle
    assert config.capture.node_lifecycle
    assert config.capture.nested_graphs
    assert config.capture.child_events == "tool_llm_metadata"
    assert config.capture.state_diff == "allowlisted_node_update"


def test_default_config_uses_a_registered_policy():
    assert default_trace_config().redaction_policy_id in REGISTERED_POLICY_IDS


def test_default_config_can_be_built_disabled():
    assert default_trace_config(enabled=False).enabled is False


# --- validation --------------------------------------------------------------

def test_none_means_no_preference_not_off():
    # Switching tracing off for one run is {"enabled": false}, a deliberate
    # statement. Omitting the block entirely must not silently disable it.
    assert validate_trace_config(None).enabled is True


def test_an_already_validated_config_passes_through():
    config = default_trace_config()
    assert validate_trace_config(config) is config


def test_a_plain_dict_is_validated():
    payload = default_trace_config().model_dump(mode="json")
    assert validate_trace_config(payload).enabled is True


@pytest.mark.parametrize("payload", ["a string", 7, [], True])
def test_a_non_object_is_rejected(payload):
    with pytest.raises(TraceConfigError) as exc:
        validate_trace_config(payload)
    assert exc.value.code == "invalid_trace_config"


def test_unregistered_policy_is_rejected_by_name():
    payload = default_trace_config().model_dump(mode="json")
    payload["redaction_policy_id"] = "something-permissive-v9"
    with pytest.raises(TraceConfigError) as exc:
        validate_trace_config(payload)
    assert exc.value.code == "unknown_redaction_policy"


def test_registered_policy_set_is_not_empty_and_contains_the_default():
    assert DEFAULT_POLICY_ID in REGISTERED_POLICY_IDS


@pytest.mark.parametrize("days", [0, -1, MAX_RETENTION_DAYS + 1, 365])
def test_retention_outside_the_window_is_rejected(days):
    payload = default_trace_config().model_dump(mode="json")
    payload["retention_days"] = days
    with pytest.raises(TraceConfigError) as exc:
        validate_trace_config(payload)
    assert exc.value.code == "invalid_trace_config"


def test_limit_above_the_hard_cap_is_rejected():
    payload = default_trace_config().model_dump(mode="json")
    payload["limits"]["max_events"] = MAX_EVENTS + 1
    with pytest.raises(TraceConfigError):
        validate_trace_config(payload)


def test_a_missing_capture_key_is_rejected():
    payload = default_trace_config().model_dump(mode="json")
    del payload["capture"]["state_diff"]
    with pytest.raises(TraceConfigError):
        validate_trace_config(payload)


def test_error_detail_never_echoes_the_submitted_value():
    # A caller may put user text in correlation; the error must not repeat it.
    secret = "spicy sichuan in chinatown for my anniversary"
    payload = default_trace_config().model_dump(mode="json")
    payload["correlation"] = {"fixture_id": secret, "case_id": None}
    with pytest.raises(TraceConfigError) as exc:
        validate_trace_config(payload)
    assert secret not in exc.value.detail
    assert secret not in str(exc.value)
    assert "correlation" in exc.value.detail


def test_error_detail_names_where_the_problem_is():
    payload = default_trace_config().model_dump(mode="json")
    payload["retention_days"] = 99
    with pytest.raises(TraceConfigError) as exc:
        validate_trace_config(payload)
    assert "retention_days" in exc.value.detail


# --- effective limits --------------------------------------------------------

def test_effective_limits_match_a_full_request():
    config = default_trace_config()
    assert effective_limits(config)["max_events"] == MAX_EVENTS


def test_effective_limits_honour_a_smaller_request():
    payload = default_trace_config().model_dump(mode="json")
    payload["limits"] = {key: 3 for key in payload["limits"]}
    config = validate_trace_config(payload)
    assert set(effective_limits(config).values()) == {3}


def test_retention_days_helper_matches_the_request():
    payload = default_trace_config().model_dump(mode="json")
    payload["retention_days"] = 1
    assert retention_days(validate_trace_config(payload)) == 1


def test_config_model_itself_still_refuses_unknown_fields():
    payload = default_trace_config().model_dump(mode="json")
    payload["capture_everything"] = True
    with pytest.raises(ValidationError):
        GraphTraceConfigV1.model_validate(payload)
