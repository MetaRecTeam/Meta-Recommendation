"""B2 privacy tests: nothing the policy withholds may appear anywhere in a projection.

These are the tests that protect every later PR. The store, the API and the UI
all carry whatever the projector lets out, so if a value escapes here it
escapes everywhere. Each test seeds a distinctive value and then scans the
*whole* projection, serialised, rather than checking the one field it expected
the value to be in.
"""
import asyncio
import json
import re

import pytest

from langgraph_metarec.wp8_trace.interfaces import Projection
from langgraph_metarec.wp8_trace.models import MAX_COLLECTION_ITEMS, MAX_NESTING_DEPTH, MAX_STRING_CHARS
from langgraph_metarec.wp8_trace.redaction import (
    ALLOWLIST,
    DENIED_KEYS,
    ERROR_CODES,
    POLICY_ID,
    Bounds,
    SafeProjectorV1,
    safe_error,
)

pytestmark = pytest.mark.backend_unit

ORCH = "request-orchestrator@v1"
ROUTING = "routing-graph@v1"
RESTAURANT = "restaurant-domain@v1"
GENERIC = "generic-domain@v1"


@pytest.fixture
def projector():
    return SafeProjectorV1()


def dumped(projection: Projection) -> str:
    """The entire projection as one string, for an absence scan."""
    def encode(value):
        for attr in ("model_dump", "__dict__"):
            got = getattr(value, attr, None)
            if callable(got):
                return got()
            if got is not None:
                return got
        return str(value)
    return json.dumps(projection, default=encode)


def project(projector, **kwargs):
    kwargs.setdefault("event_type", "node_start")
    kwargs.setdefault("runnable_type", "graph_node")
    kwargs.setdefault("graph_definition_id", ORCH)
    kwargs.setdefault("node_id", "intention")
    return projector.project(**kwargs)


# --- denied keys -------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(DENIED_KEYS))
def test_every_denied_key_is_withheld_at_the_top_level(projector, key):
    secret = f"SEEDED-{key.upper()}-VALUE"
    result = project(projector, node_input={key: secret, "use_online_agent": True})
    assert secret not in dumped(result)


@pytest.mark.parametrize("key", sorted(DENIED_KEYS))
def test_every_denied_key_is_withheld_inside_an_allowlisted_object(projector, key):
    # Stage 2 exists for exactly this: a new field added inside an object the
    # allowlist already keeps must still be refused.
    secret = f"NESTED-{key.upper()}-VALUE"
    result = project(projector, node_input={"runtime": {"branch_id": "main", key: secret}})
    assert secret not in dumped(result)


def test_thread_id_is_withheld_because_it_contains_the_ids_it_hides(projector):
    # thread_id is built as f"{user_id}:{conversation_id}:{branch_id}".
    # Keeping it while omitting user_id would leak exactly what was hidden.
    result = project(projector, node_input={
        "runtime": {"user_id": "u-12345", "conversation_id": "conv-98765",
                    "thread_id": "u-12345:conv-98765:main", "branch_id": "main"}})
    text = dumped(result)
    assert "u-12345" not in text
    assert "conv-98765" not in text
    assert "thread_id" in DENIED_KEYS and "task_thread_id" in DENIED_KEYS


def test_the_user_query_never_survives(projector):
    query = "halal japanese near bugis under 30 dollars for my girlfriend"
    result = project(projector, node_input={"runtime": {"query": query}, "query": query})
    assert query not in dumped(result)
    for word in ("halal", "bugis", "girlfriend"):
        assert word not in dumped(result)


def test_conversation_history_and_profile_never_survive(projector):
    result = project(projector, node_input={
        "conversation_history": ["I am lactose intolerant", "my budget is 20"],
        "user_profile": {"diet": "halal", "home": "Tampines"},
        "use_online_agent": True})
    text = dumped(result)
    for leak in ("lactose", "budget", "halal", "Tampines"):
        assert leak not in text
    assert result.input_summary.keys == ["use_online_agent"]  # the safe sibling survived


# --- value patterns ----------------------------------------------------------

@pytest.mark.parametrize("value", [
    "hayden@example.com",
    "contact me at a.b-c@sub.domain.org please",
    "S1234567D",
    "+65 9123 4567",
    "91234567",
    "4111 1111 1111 1111",
    "Bearer abcdefghijklmnop",
    "sk-abcdefghijklmnopqrst",
    "https://maps.example.com/place?lat=1.3&lng=103.8&user=hayden",
])
def test_a_pattern_match_is_dropped_even_from_an_allowlisted_field(projector, value):
    # domain_reason is allowlisted and is f-string built, so it is the field
    # most likely to carry something it should not one day.
    result = project(projector, graph_definition_id=ROUTING,
                     node_input={"domain_reason": value, "domain": "restaurant"})
    assert value not in dumped(result)
    assert "domain_reason" not in result.input_summary.keys
    assert "domain" in result.input_summary.keys  # the clean sibling survived
    assert "domain_reason" in result.redacted_paths


def test_a_clean_allowlisted_reason_is_kept(projector):
    result = project(projector, graph_definition_id=ROUTING,
                     node_input={"domain_reason": "matched multiple domains: restaurant, hotel"})
    assert result.input_summary is not None
    assert "domain_reason" in result.input_summary.keys


# --- structural bounds -------------------------------------------------------

def test_a_long_string_is_truncated_and_the_path_recorded(projector):
    result = project(projector, graph_definition_id=ROUTING,
                     node_input={"domain_reason": "a" * (MAX_STRING_CHARS + 500)})
    assert "domain_reason" in result.truncated_paths


def test_a_big_collection_is_cut_to_the_limit(projector):
    result = project(projector, graph_definition_id=ROUTING,
                     node_input={"detected_domains": [f"d{i}" for i in range(MAX_COLLECTION_ITEMS + 40)]})
    assert "detected_domains" in result.truncated_paths


def test_requested_lower_bounds_are_honoured():
    tight = SafeProjectorV1(bounds=Bounds(max_string_chars=8, max_collection_items=2, max_nesting_depth=2))
    result = tight.project(event_type="node_start", runnable_type="graph_node",
                           graph_definition_id=ROUTING, node_id="routing",
                           node_input={"domain_reason": "x" * 50,
                                       "detected_domains": ["a", "b", "c", "d"]})
    assert "domain_reason" in result.truncated_paths
    assert "detected_domains" in result.truncated_paths


def test_nesting_deeper_than_the_limit_is_refused(projector):
    deep = value = {}
    for _ in range(MAX_NESTING_DEPTH + 3):
        value["next"] = {}
        value = value["next"]
    value["leak"] = "DEEP-SECRET"
    result = project(projector, graph_definition_id=RESTAURANT,
                     node_input={"rejection_stats": deep})
    assert "DEEP-SECRET" not in dumped(result)


def test_a_non_finite_number_is_refused(projector):
    result = project(projector, graph_definition_id=GENERIC,
                     node_input={"candidate_count": float("inf"), "domain": "generic"})
    assert result.input_summary is not None
    assert "candidate_count" not in result.input_summary.keys


# --- what the allowlist keeps ------------------------------------------------

def test_counts_booleans_and_enums_survive(projector):
    result = project(projector, graph_definition_id=GENERIC, node_input={
        "domain": "generic", "use_online_agent": False,
        "gather_iterations": 3, "candidate_count": 42,
        "tool_tags": ["search", "rank"]})
    kept = set(result.input_summary.keys)
    assert kept == {"domain", "use_online_agent", "gather_iterations", "candidate_count", "tool_tags"}


def test_rejection_stats_survive_because_they_are_counts(projector):
    result = project(projector, graph_definition_id=RESTAURANT,
                     node_input={"rejection_stats": {"closed": 4, "too_far": 9}})
    assert "rejection_stats" in result.input_summary.keys


def test_a_results_list_becomes_a_count_not_its_contents(projector):
    result = project(projector, graph_definition_id=RESTAURANT, node_input={
        "restaurants": [{"name": "Hai Di Lao", "address": "313 Somerset"},
                        {"name": "Tim Ho Wan", "address": "68 Orchard Rd"}]})
    text = dumped(result)
    assert "Hai Di Lao" not in text and "Somerset" not in text
    assert "restaurants" in result.input_summary.keys


def test_an_unknown_field_is_omitted_rather_than_kept(projector):
    result = project(projector, graph_definition_id=GENERIC,
                     node_input={"domain": "generic", "a_field_added_next_year": "SURPRISE"})
    assert "SURPRISE" not in dumped(result)


def test_an_unregistered_graph_keeps_nothing(projector):
    result = project(projector, graph_definition_id="not-registered@v1",
                     node_input={"anything": "AT-ALL"})
    assert "AT-ALL" not in dumped(result)
    assert result.input_summary is None


# --- summaries describe without carrying -------------------------------------

def test_a_summary_reports_names_and_sizes_not_values(projector):
    result = project(projector, graph_definition_id=GENERIC,
                     node_input={"domain": "generic", "candidate_count": 7})
    summary = result.input_summary
    assert summary.item_count == 2
    assert summary.bytes > 0
    assert set(summary.value_types) == {"domain", "candidate_count"}
    assert summary.value_types["candidate_count"] == "int"


def test_omitted_count_reports_how_much_was_withheld(projector):
    result = project(projector, node_input={
        "use_online_agent": True, "query": "x", "user_profile": {}, "conversation_history": []})
    assert result.input_summary.omitted_count == 3


# --- state diff --------------------------------------------------------------

def test_a_diff_appears_only_on_node_end(projector):
    args = dict(graph_definition_id=GENERIC, node_id="gather",
                node_input={"candidate_count": 0}, output={"candidate_count": 5})
    assert project(projector, event_type="node_start", **args).state_diff is None
    assert project(projector, event_type="node_end", **args).state_diff is not None


def test_a_failed_node_has_no_diff(projector):
    result = project(projector, event_type="node_end", graph_definition_id=GENERIC,
                     node_id="gather", node_input={"candidate_count": 0},
                     error=RuntimeError("boom"))
    assert result.state_diff is None


def test_diff_classifies_added_changed_and_removed(projector):
    result = project(projector, event_type="node_end", graph_definition_id=GENERIC,
                     node_id="gather",
                     node_input={"candidate_count": 2, "gather_iterations": 1, "domain": "generic"},
                     output={"candidate_count": 9, "gather_iterations": None, "tool_tags": ["search"]})
    diff = result.state_diff
    assert diff.basis == "node_input_to_update"
    assert diff.added == {"tool_tags": ["search"]}
    assert diff.changed["candidate_count"].before == 2
    assert diff.changed["candidate_count"].after == 9
    assert diff.removed == ["gather_iterations"]


def test_a_field_the_update_did_not_mention_is_not_a_removal(projector):
    # Absence of news is not a deletion (DECISIONS.md R3).
    result = project(projector, event_type="node_end", graph_definition_id=GENERIC,
                     node_id="gather", node_input={"candidate_count": 2, "domain": "generic"},
                     output={"candidate_count": 3})
    assert result.state_diff.removed == []


def test_diff_omissions_carry_a_machine_reason(projector):
    result = project(projector, event_type="node_end", graph_definition_id=GENERIC,
                     node_id="gather", node_input={"query": "secret words"},
                     output={"candidate_count": 1})
    pattern = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
    assert result.state_diff.omitted
    for omission in result.state_diff.omitted:
        assert pattern.match(omission.reason), omission.reason


def test_an_omission_is_a_marker_never_a_hash(projector):
    secret = "a-very-distinctive-secret-value"
    result = project(projector, node_input={"query": secret})
    text = dumped(result)
    assert secret not in text
    # No hash of it either: a hash of a short value is guessable, and any hash
    # still proves two traces saw the same thing.
    import hashlib
    for algo in ("md5", "sha1", "sha256"):
        digest = getattr(hashlib, algo)(secret.encode()).hexdigest()
        assert digest not in text
        assert digest[:16] not in text


# --- child spans -------------------------------------------------------------

@pytest.mark.parametrize("event_type,field", [("tool_start", "tool"), ("llm_start", "model")])
def test_a_child_span_carries_only_a_name(projector, event_type, field):
    result = project(projector, event_type=event_type, runnable_type="tool",
                     raw_name="restaurant_search")
    assert getattr(result.metadata, field) == "restaurant_search"
    assert result.input_summary is None and result.output_summary is None


def test_a_child_span_refuses_a_payload_even_if_one_is_passed(projector):
    # Prompts and tool arguments should never reach the projector. If they do,
    # they still must not be stored.
    result = project(projector, event_type="llm_start", runnable_type="llm",
                     raw_name="gpt-4o-mini",
                     node_input={"prompt": "the user said: I live in Tampines"})
    assert "Tampines" not in dumped(result)


# --- safe errors -------------------------------------------------------------

def test_an_exception_message_never_reaches_a_trace():
    leak = "connection to postgres://admin:hunter2@10.0.0.4/metarec failed"
    error = safe_error(RuntimeError(leak))
    assert leak not in json.dumps(error.model_dump())
    assert "hunter2" not in error.safe_message


@pytest.mark.parametrize("exc,code", [
    (asyncio.CancelledError(), "cancelled"),
    (TimeoutError(), "timeout"),
    (ValueError("bad"), "validation_failed"),
    (ConnectionError("down"), "provider_unavailable"),
    (RuntimeError("other"), "execution_failed"),
])
def test_exceptions_classify_to_the_reviewed_codes(exc, code):
    assert safe_error(exc).code == code


def test_every_code_has_a_fixed_message_with_nothing_interpolated():
    for code, (message, retryable) in ERROR_CODES.items():
        assert re.match(r"^[a-z][a-z0-9_]{0,63}$", code)
        assert "{" not in message and "%" not in message
        assert isinstance(retryable, bool)


def test_retryable_is_a_property_of_the_code_not_the_instance():
    assert safe_error(TimeoutError()).retryable is True
    assert safe_error(ValueError("x")).retryable is False


def test_a_weird_exception_class_name_is_reduced_to_safe_characters():
    cls = type("Bad Name<with>chars", (RuntimeError,), {})
    assert re.match(r"^[A-Za-z0-9._:@/-]{1,128}$", safe_error(cls("x")).error_class)


# --- the policy itself -------------------------------------------------------

def test_policy_id_is_the_registered_one():
    from langgraph_metarec.wp8_trace.config import REGISTERED_POLICY_IDS
    assert POLICY_ID in REGISTERED_POLICY_IDS
    assert SafeProjectorV1().policy_id == POLICY_ID


def test_every_registered_graph_has_an_allowlist():
    from langgraph_metarec.wp8_trace.topology import graph_definition_ids
    assert set(graph_definition_ids()) == set(ALLOWLIST)


def test_no_allowlist_entry_names_a_denied_key():
    # A contradiction between the two stages would be silently resolved by
    # stage 2, which is correct but hides an authoring mistake.
    for graph, rules in ALLOWLIST.items():
        assert not (set(rules) & DENIED_KEYS), graph


def test_the_projector_satisfies_the_agreed_interface():
    # SafeProjector is a plain Protocol, so this checks the shape A agreed to
    # in R16 rather than a runtime isinstance.
    import inspect

    from langgraph_metarec.wp8_trace.interfaces import SafeProjector

    ours = inspect.signature(SafeProjectorV1.project)
    theirs = inspect.signature(SafeProjector.project)
    assert set(theirs.parameters) <= set(ours.parameters)
    for name, parameter in theirs.parameters.items():
        if name == "self":
            continue
        assert ours.parameters[name].kind == parameter.kind, name
    assert isinstance(SafeProjectorV1().policy_id, str)
