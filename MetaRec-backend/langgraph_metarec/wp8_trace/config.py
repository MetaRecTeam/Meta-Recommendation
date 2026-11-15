"""WP8 trace configuration: the server flag, and validation of a capture request.

Three gates stand between a caller and a stored trace, and all three must be
open (WP8 brief, invariant 5):

1. ``DEBUG_UI_ENABLED`` — the Debug Arena exists at all.
2. ``DEBUG_GRAPH_TRACE_ENABLED`` — graph tracing exists. **Defaults to off.**
3. ``GraphTraceConfigV1.enabled`` — this particular run asked to be traced.

A request's ``enabled: true`` cannot open gate 2; the server flag always wins,
so turning the feature off at the deployment is sufficient and no client can
talk its way past it.

Nothing here imports from the recommendation path, and nothing here runs
unless a debug route calls it.
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from pydantic import ValidationError

from .models import (
    DEFAULT_RETENTION_DAYS,
    MAX_COLLECTION_ITEMS,
    MAX_EVENT_BYTES,
    MAX_EVENTS,
    MAX_NESTING_DEPTH,
    MAX_RETENTION_DAYS,
    MAX_STRING_CHARS,
    MAX_TRACE_BYTES,
    GraphTraceConfigV1,
)

GRAPH_TRACE_FLAG = "DEBUG_GRAPH_TRACE_ENABLED"

#: Policies the server will honour. An unregistered id is rejected rather than
#: silently falling back, because a trace stored under a policy nobody can name
#: cannot be audited later.
REGISTERED_POLICY_IDS = frozenset({"metarec-debug-safe-v1"})
DEFAULT_POLICY_ID = "metarec-debug-safe-v1"


class TraceConfigError(ValueError):
    """A capture request that cannot be honoured, with a safe reason code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def graph_trace_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """The server flag. Absent means off, which is the safe direction."""
    raw = (env if env is not None else os.environ).get(GRAPH_TRACE_FLAG)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def default_trace_config(*, enabled: bool = True) -> GraphTraceConfigV1:
    """A full-capture request at the hard caps, used when a caller sends none."""
    return GraphTraceConfigV1.model_validate(
        {
            "schema_version": "graph-trace-config/1.0",
            "enabled": enabled,
            "capture": {
                "graph_lifecycle": True,
                "node_lifecycle": True,
                "nested_graphs": True,
                "child_events": "tool_llm_metadata",
                "state_diff": "allowlisted_node_update",
            },
            "redaction_policy_id": DEFAULT_POLICY_ID,
            "limits": {
                "max_events": MAX_EVENTS,
                "max_event_bytes": MAX_EVENT_BYTES,
                "max_trace_bytes": MAX_TRACE_BYTES,
                "max_string_chars": MAX_STRING_CHARS,
                "max_collection_items": MAX_COLLECTION_ITEMS,
                "max_nesting_depth": MAX_NESTING_DEPTH,
            },
            "retention_days": DEFAULT_RETENTION_DAYS,
            "correlation": None,
        }
    )


def validate_trace_config(payload: Any) -> GraphTraceConfigV1:
    """Validate a capture request, or raise ``TraceConfigError`` with a code.

    ``None`` means "no preference", not "off": the caller gets the default
    full-capture config. Switching tracing off for one run is
    ``{"enabled": false}``, which is a deliberate statement rather than an
    omission.

    The model already enforces the shape, the hard caps and ``retention_days``
    1-30. This adds the one rule a schema cannot express: the policy id must be
    one the server actually has.
    """
    if payload is None:
        return default_trace_config()
    if isinstance(payload, GraphTraceConfigV1):
        config = payload
    else:
        if not isinstance(payload, Mapping):
            raise TraceConfigError("invalid_trace_config", "trace config must be an object")
        try:
            config = GraphTraceConfigV1.model_validate(payload)
        except ValidationError as exc:
            raise TraceConfigError(
                "invalid_trace_config", _first_problem(exc)
            ) from None
    if config.redaction_policy_id not in REGISTERED_POLICY_IDS:
        raise TraceConfigError(
            "unknown_redaction_policy",
            f"redaction policy {config.redaction_policy_id!r} is not registered",
        )
    return config


def _first_problem(exc: ValidationError) -> str:
    """One readable line from a validation error, carrying no submitted values.

    Pydantic puts the offending input in ``ctx`` and in the message for some
    error types. A debug caller may have sent a correlation id that is user
    text, so the location and the rule are echoed, never the value.
    """
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "(root)"
    return f"{where}: {first['type']}"


def effective_limits(config: GraphTraceConfigV1) -> dict[str, int]:
    """The limits actually applied: the request's, floored by the hard caps.

    The model already refuses anything above a cap, so this cannot raise one.
    It exists so every caller reads limits the same way and nobody re-derives
    them from ``MAX_*`` and forgets the request asked for less.
    """
    return {
        "max_events": min(config.limits.max_events, MAX_EVENTS),
        "max_event_bytes": min(config.limits.max_event_bytes, MAX_EVENT_BYTES),
        "max_trace_bytes": min(config.limits.max_trace_bytes, MAX_TRACE_BYTES),
        "max_string_chars": min(config.limits.max_string_chars, MAX_STRING_CHARS),
        "max_collection_items": min(config.limits.max_collection_items, MAX_COLLECTION_ITEMS),
        "max_nesting_depth": min(config.limits.max_nesting_depth, MAX_NESTING_DEPTH),
    }


def retention_days(config: GraphTraceConfigV1) -> int:
    return min(config.retention_days, MAX_RETENTION_DAYS)
