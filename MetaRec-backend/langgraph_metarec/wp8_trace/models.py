"""WP8 v1 contract models.

Topology models (``GraphTraceV1.topology``) and the event model
(``GraphRunEventV1``) live here; Member B adds the config, page and diff
models. See docs/wp8/team/FRAMEWORK.md §4 and the WP8 brief's contract
section, which is normative.

These models check shape and the hard caps. Which values a policy lets
through, and the lower requested limits, are enforced by the redaction
pipeline before an event is built.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

GRAPH_DEFINITION_ID_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*@v[1-9][0-9]*$"

# Hard caps from the WP8 brief. Deployments may set lower limits; never higher.
MAX_STRING_CHARS = 512
MAX_COLLECTION_ITEMS = 50
MAX_NESTING_DEPTH = 6

TopologyNodeKind = Literal["start", "end", "node", "tool", "llm"]
TopologyEdgeKind = Literal["normal", "conditional"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TopologyNode(_Strict):
    node_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    kind: TopologyNodeKind


class TopologyEdge(_Strict):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    kind: TopologyEdgeKind
    # A stable route key from the graph code for conditional edges; never
    # executable source or user data. Always null for normal edges.
    label: Optional[str] = None


class GraphDefinition(_Strict):
    """Possible nodes and edges of one compiled graph. Not what ran."""

    graph_definition_id: str = Field(pattern=GRAPH_DEFINITION_ID_PATTERN)
    name: str = Field(min_length=1)
    nodes: List[TopologyNode]
    edges: List[TopologyEdge]


class GraphInstance(_Strict):
    """One execution of one graph definition inside a trace."""

    graph_instance_id: str = Field(min_length=1)
    graph_definition_id: str = Field(pattern=GRAPH_DEFINITION_ID_PATTERN)
    parent_graph_instance_id: Optional[str]
    parent_node_run_id: Optional[str]
    namespace: List[str]
    started_sequence: int = Field(ge=1)
    ended_sequence: Optional[int] = Field(ge=1)


# ---------------------------------------------------------------------------
# GraphRunEventV1
# ---------------------------------------------------------------------------

EventType = Literal[
    "graph_start", "graph_end", "node_start", "node_end",
    "tool_start", "tool_end", "llm_start", "llm_end",
    "policy_start", "policy_end", "error", "trace_truncated",
]
EventStatus = Literal["running", "completed", "error", "cancelled", "incomplete"]
RunnableType = Literal[
    "graph", "graph_node", "tool", "llm", "policy",
    "llm_attempt_adapter", "tool_scope_adapter", "trace_system",
]

SafeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:@/-]{1,128}$")]
BoundedStr = Annotated[str, Field(max_length=MAX_STRING_CHARS)]
BoundedStrList = Annotated[List[BoundedStr], Field(max_length=MAX_COLLECTION_ITEMS)]
SafeIdList = Annotated[List[SafeId], Field(max_length=MAX_COLLECTION_ITEMS)]


def _check_safe_value(value: Any, depth: int = 0) -> Any:
    """null, bool, finite number, bounded string, or bounded list/object.

    A safe value holds at most MAX_NESTING_DEPTH levels of lists/objects.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("safe values must be finite numbers")
        return value
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            raise ValueError(f"safe strings are at most {MAX_STRING_CHARS} characters")
        return value
    if isinstance(value, (list, dict)):
        if depth >= MAX_NESTING_DEPTH:
            raise ValueError(f"safe values nest at most {MAX_NESTING_DEPTH} levels")
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ValueError(f"safe collections hold at most {MAX_COLLECTION_ITEMS} items")
        if isinstance(value, list):
            for item in value:
                _check_safe_value(item, depth + 1)
        else:
            for key, item in value.items():
                if not isinstance(key, str) or len(key) > MAX_STRING_CHARS:
                    raise ValueError("safe object keys are bounded strings")
                _check_safe_value(item, depth + 1)
        return value
    raise ValueError(f"{type(value).__name__} is not a safe value")


_SAFE_VALUE_MARKER = "x-wp8-safe-value"

SafeValue = Annotated[
    Any,
    AfterValidator(_check_safe_value),
    # contract_json_schema() swaps this marker for the depth-limited $defs below.
    WithJsonSchema({
        _SAFE_VALUE_MARKER: True,
        "description": (
            f"null, boolean, finite number, string of at most {MAX_STRING_CHARS} characters, "
            f"or list/object of at most {MAX_COLLECTION_ITEMS} safe values, nested at most "
            f"{MAX_NESTING_DEPTH} levels"
        ),
    }),
]
SafeObject = Annotated[Dict[BoundedStr, SafeValue], Field(max_length=MAX_COLLECTION_ITEMS)]


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be UTC")
    return value


UtcDatetime = Annotated[
    datetime,
    AfterValidator(_utc),
    WithJsonSchema({"type": "string", "format": "date-time", "pattern": "(Z|[+-]00:00)$"}),
]


class PayloadSummary(_Strict):
    keys: BoundedStrList
    bytes: int = Field(ge=0)
    value_types: Optional[Annotated[Dict[BoundedStr, BoundedStr], Field(max_length=MAX_COLLECTION_ITEMS)]] = None
    item_count: Optional[int] = Field(default=None, ge=0)
    omitted_count: Optional[int] = Field(default=None, ge=0)


class ChangedValue(_Strict):
    before: SafeValue
    after: SafeValue


class Omission(_Strict):
    path: BoundedStr
    reason: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class StateDiff(_Strict):
    basis: Literal["node_input_to_update"]
    added: SafeObject
    changed: Annotated[Dict[BoundedStr, ChangedValue], Field(max_length=MAX_COLLECTION_ITEMS)]
    # Only explicit deletion/checkpoint facts; never inferred from an omitted update.
    removed: BoundedStrList
    omitted: Annotated[List[Omission], Field(max_length=MAX_COLLECTION_ITEMS)]


class LlmAttempt(_Strict):
    """WP5 physical attempt. Start-only events leave the outcome fields null."""

    request_id: Optional[SafeId]
    attempt_id: Optional[SafeId]
    provider_id: Optional[SafeId]
    model_id: Optional[SafeId]
    policy_leg: Optional[SafeId]
    outcome: Optional[SafeId]
    validation_status: Optional[SafeId]
    total_tokens: Optional[int] = Field(ge=0)
    cost_usd: Optional[float] = Field(ge=0, allow_inf_nan=False)
    pricing_snapshot_id: Optional[SafeId]
    provider_price_table_version: Optional[SafeId]


class ExcludedTool(_Strict):
    name: SafeId
    reason_code: SafeId


class ToolScope(_Strict):
    """WP7 pre-LLM tool-scope decision."""

    catalog_id: Optional[SafeId]
    health_snapshot_id: Optional[SafeId]
    policy_version: Optional[SafeId]
    eligible_tool_names: SafeIdList
    excluded: Annotated[List[ExcludedTool], Field(max_length=MAX_COLLECTION_ITEMS)]


class ToolHealth(_Strict):
    """Provider health only; never probe bodies, credentials or quota."""

    provider_id: SafeId
    state: SafeId
    reason_code: Optional[SafeId]
    checked_at: Optional[UtcDatetime]
    expires_at: Optional[UtcDatetime]


class EventMetadata(_Strict):
    domain: Optional[SafeId] = None
    tool: Optional[SafeId] = None
    model: Optional[SafeId] = None
    attempt: Optional[int] = Field(default=None, ge=1)
    task_id: Optional[SafeId] = None
    llm_attempt: Optional[LlmAttempt] = None
    tool_scope: Optional[ToolScope] = None
    tool_health: Optional[ToolHealth] = None


class SafeError(_Strict):
    code: SafeId
    error_class: SafeId
    safe_message: BoundedStr
    retryable: bool


class RedactionInfo(_Strict):
    policy_id: SafeId
    redacted_paths: BoundedStrList
    truncated_paths: BoundedStrList
    stored_bytes: int = Field(ge=0)


# event_type -> the runnable_type values it may carry
_RUNNABLE_TYPES: Dict[str, frozenset] = {
    "graph_start": frozenset({"graph"}),
    "graph_end": frozenset({"graph"}),
    "node_start": frozenset({"graph_node"}),
    "node_end": frozenset({"graph_node"}),
    "tool_start": frozenset({"tool"}),
    "tool_end": frozenset({"tool"}),
    "llm_start": frozenset({"llm", "llm_attempt_adapter"}),
    "llm_end": frozenset({"llm", "llm_attempt_adapter"}),
    "policy_start": frozenset({"policy", "tool_scope_adapter"}),
    "policy_end": frozenset({"policy", "tool_scope_adapter"}),
    "error": frozenset({"graph", "graph_node", "tool", "llm", "policy", "llm_attempt_adapter", "tool_scope_adapter"}),
    "trace_truncated": frozenset({"trace_system"}),
}
_CHILD_SPAN_EVENTS = frozenset({"tool_start", "tool_end", "llm_start", "llm_end", "policy_start", "policy_end"})

_NULL = {"type": "null"}
_NOT_NULL = {"not": {"type": "null"}}


def _when(condition: Dict[str, Any], then: Dict[str, Any]) -> Dict[str, Any]:
    return {"if": {"properties": condition, "required": list(condition)}, "then": {"properties": then}}


def _matrix_json_schema() -> List[Dict[str, Any]]:
    """The required/null matrix as JSON Schema 2020-12 if/then rules.

    Mirrors GraphRunEventV1._required_null_matrix so a consumer validating
    with the checked-in schema alone rejects the same events Python does.
    """
    rules: List[Dict[str, Any]] = []
    for kind, runnable in _RUNNABLE_TYPES.items():
        then: Dict[str, Any] = {"runnable_type": {"enum": sorted(runnable)}}
        if kind == "trace_truncated":
            then.update(node_id=_NULL, run_id=_NULL, parent_run_id=_NULL)
        else:
            then.update(graph_instance_id=_NOT_NULL, run_id=_NOT_NULL)
        if kind in ("graph_start", "graph_end"):
            then["node_id"] = _NULL
        elif kind in ("node_start", "node_end") or kind in _CHILD_SPAN_EVENTS:
            then.update(node_id=_NOT_NULL, parent_run_id=_NOT_NULL)
        elif kind == "error":
            then.update(error=_NOT_NULL, status={"enum": ["cancelled", "error"]})
        if kind.endswith("_start"):
            then.update(duration_ms=_NULL, status={"const": "running"})
        elif kind.endswith("_end"):
            then["status"] = {"not": {"const": "running"}}
        if kind != "node_end":
            then["state_diff"] = _NULL
        rules.append(_when({"event_type": {"const": kind}}, then))
    rules += [
        _when({"event_type": {"const": "error"}, "runnable_type": {"not": {"const": "graph"}}},
              {"node_id": _NOT_NULL, "parent_run_id": _NOT_NULL}),
        _when({"graph_instance_id": {"type": "string"}}, {"graph_definition_id": _NOT_NULL}),
        _when({"parent_graph_instance_id": {"type": "string"}}, {"parent_run_id": _NOT_NULL}),
    ]
    for key, allowed in (
        ("llm_attempt", ["llm_attempt_adapter"]),
        ("tool_scope", ["tool_scope_adapter"]),
        ("tool_health", ["tool", "tool_scope_adapter"]),
    ):
        present = {"type": "object", "required": [key], "properties": {key: {"type": "object"}}}
        rules.append(_when({"metadata": present}, {"runnable_type": {"enum": allowed}}))
    return rules


class GraphRunEventV1(_Strict):
    """One accepted trace event. Every key is present; unavailable values are null."""

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra={"allOf": _matrix_json_schema()})

    schema_version: Literal["graph-run-event/1.0"]
    event_id: SafeId
    trace_id: SafeId
    debug_run_id: SafeId
    sequence: int = Field(ge=1)
    timestamp_utc: UtcDatetime
    monotonic_offset_ms: float = Field(ge=0, allow_inf_nan=False)
    event_type: EventType
    status: EventStatus
    graph_definition_id: Optional[Annotated[str, Field(pattern=GRAPH_DEFINITION_ID_PATTERN)]]
    graph_instance_id: Optional[SafeId]
    parent_graph_instance_id: Optional[SafeId]
    node_id: Optional[SafeId]
    run_id: Optional[SafeId]
    parent_run_id: Optional[SafeId]
    runnable_type: RunnableType
    duration_ms: Optional[float] = Field(ge=0, allow_inf_nan=False)
    input_summary: Optional[PayloadSummary]
    output_summary: Optional[PayloadSummary]
    state_diff: Optional[StateDiff]
    metadata: Optional[EventMetadata]
    error: Optional[SafeError]
    redaction: RedactionInfo

    @model_validator(mode="after")
    def _required_null_matrix(self) -> "GraphRunEventV1":
        """The WP8 brief's required/null matrix, per event type."""
        kind = self.event_type
        problems: List[str] = []

        def need(field: str) -> None:
            if getattr(self, field) is None:
                problems.append(f"{kind} requires {field}")

        def forbid(field: str, why: str) -> None:
            if getattr(self, field) is not None:
                problems.append(f"{kind} must have null {field} ({why})")

        if self.runnable_type not in _RUNNABLE_TYPES[kind]:
            problems.append(f"{kind} cannot have runnable_type {self.runnable_type}")

        if kind == "trace_truncated":
            for field in ("node_id", "run_id", "parent_run_id"):
                forbid(field, "a collector marker, not a lifecycle span")
        else:
            need("graph_instance_id")
            need("run_id")

        if kind in ("graph_start", "graph_end"):
            forbid("node_id", "graph lifecycle")
        elif kind in ("node_start", "node_end") or kind in _CHILD_SPAN_EVENTS:
            # Node parent = containing graph run; child spans = exactly the triggering node run.
            need("node_id")
            need("parent_run_id")
        elif kind == "error":
            need("error")
            if self.status not in ("error", "cancelled"):
                problems.append("error events have status error or cancelled")
            if self.runnable_type != "graph":
                need("node_id")
                need("parent_run_id")

        if self.graph_instance_id is not None:
            need("graph_definition_id")
        if self.parent_graph_instance_id is not None and self.parent_run_id is None:
            problems.append("a nested graph links to its invoking node run, so parent_run_id is required")

        if kind.endswith("_start"):
            forbid("duration_ms", "durations exist only on a matched end")
            if self.status != "running":
                problems.append(f"{kind} has status running")
        elif kind.endswith("_end") and self.status == "running":
            problems.append(f"{kind} cannot have status running")

        if self.state_diff is not None and kind != "node_end":
            problems.append("state_diff compares a node's input with its returned update, so only node_end carries it")

        md = self.metadata
        if md is not None:
            if md.llm_attempt is not None and self.runnable_type != "llm_attempt_adapter":
                problems.append("metadata.llm_attempt belongs on WP5 llm_attempt_adapter events")
            if md.tool_scope is not None and self.runnable_type != "tool_scope_adapter":
                problems.append("metadata.tool_scope belongs on WP7 tool_scope_adapter events")
            if md.tool_health is not None and self.runnable_type not in ("tool", "tool_scope_adapter"):
                problems.append("metadata.tool_health belongs on tool or tool-scope events")

        if problems:
            raise ValueError("; ".join(problems))
        return self


# ---------------------------------------------------------------------------
# Checked-in JSON Schema
# ---------------------------------------------------------------------------


def _safe_value_defs() -> Dict[str, Any]:
    defs: Dict[str, Any] = {}
    for level in range(MAX_NESTING_DEPTH + 1):
        options: List[Dict[str, Any]] = [
            {"type": ["null", "boolean", "number"]},
            {"type": "string", "maxLength": MAX_STRING_CHARS},
        ]
        if level < MAX_NESTING_DEPTH:
            child = {"$ref": f"#/$defs/SafeValueLevel{level + 1}"}
            options += [
                {"type": "array", "maxItems": MAX_COLLECTION_ITEMS, "items": child},
                {
                    "type": "object",
                    "maxProperties": MAX_COLLECTION_ITEMS,
                    "propertyNames": {"maxLength": MAX_STRING_CHARS},
                    "additionalProperties": child,
                },
            ]
        defs[f"SafeValueLevel{level}"] = {"anyOf": options}
    return defs


def contract_json_schema(model: type) -> Dict[str, Any]:
    """The model's JSON Schema with safe values expanded to their hard caps.

    Non-finite numbers cannot be expressed in JSON, so that check stays in
    Python only.
    """
    found = False

    def swap(node: Any) -> Any:
        nonlocal found
        if isinstance(node, dict):
            if node.get(_SAFE_VALUE_MARKER):
                found = True
                return {"$ref": "#/$defs/SafeValueLevel0", "description": node["description"]}
            return {key: swap(value) for key, value in node.items()}
        if isinstance(node, list):
            return [swap(value) for value in node]
        return node

    schema = swap(model.model_json_schema())
    if found:
        schema.setdefault("$defs", {}).update(_safe_value_defs())
    return schema
