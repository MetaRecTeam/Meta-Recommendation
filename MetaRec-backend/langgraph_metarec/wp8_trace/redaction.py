"""B2 · ``metarec-debug-safe-v1``: what a trace is allowed to remember.

The pipeline runs in this order, and each stage can only ever remove:

1. **Allowlist** — a field survives only if this module names it. A field
   nobody has read is omitted, so adding state to a graph cannot widen a
   trace by accident.
2. **Denied key** — a name that must never be stored is dropped even where an
   allowlist branch would have kept it. This is the stage that catches a new
   field added inside an already-allowlisted object.
3. **Value pattern** — a kept value that *looks* like an address, a number or
   an identity document is dropped on sight, whatever its field is called.
4. **Structural bounds** — strings, collections and nesting are cut to the
   limits in force for this trace.

Omissions are recorded as markers (`path`, `reason`), never as hashes. A hash
of a short value is reversible by guessing, and a hash of any value still
proves two traces saw the same thing.

Allowlist, as decided field by field against the five graph states:

| State | Keep | Partial | Omit |
|---|---:|---:|---:|
| `GraphRuntimeState` | 4 | 5 | 10 |
| `RequestOrchestratorState` | 3 | 1 | 3 |
| `RoutingRuntimeState` | 9 | 2 | 3 |
| `GenericRuntimeState` | 5 | 3 | 6 |
| `RestaurantRuntimeState` | 5 | 4 | 10 |

The kept third is counts, booleans, enums and names of our own code: shapes
and counts, not contents.

`thread_id` is the trap. It reads as an opaque handle but is built as
``f"{user_id}:{conversation_id}:{branch_id}"`` (``checkpointing.py``), so
omitting `user_id` while keeping `thread_id` leaks exactly what was hidden.
It is in `DENIED_KEYS` for that reason, as is `task_thread_id`.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .interfaces import Projection
from .models import (
    MAX_COLLECTION_ITEMS,
    MAX_NESTING_DEPTH,
    MAX_STRING_CHARS,
    ChangedValue,
    EventMetadata,
    Omission,
    PayloadSummary,
    SafeError,
    StateDiff,
)

POLICY_ID = "metarec-debug-safe-v1"


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Keep:
    """Store this value, subject to the later stages."""


@dataclass(frozen=True)
class Nested:
    """Recurse into this object or list of objects with a narrower allowlist."""

    fields: Mapping[str, "Rule"]


@dataclass(frozen=True)
class CountOnly:
    """Store how many, never what. For lists of results or user-facing items."""


Rule = Any  # Keep | Nested | CountOnly

#: Shared by both checkpointed graphs, so the highest-traffic object here.
_RUNTIME: Mapping[str, Rule] = {
    "schema_version": Keep(),
    "branch_id": Keep(),
    "message_id": Keep(),
    "task_id": Keep(),
    "intent_result": Nested({"intent": Keep(), "confidence": Keep(), "status": Keep()}),
    "task_status": Nested({"task_id": Keep(), "status": Keep(), "progress": Keep()}),
    "progress_events": Nested({"stage": Keep(), "status": Keep(), "progress": Keep()}),
}

_DOMAIN_ROUTE: Mapping[str, Rule] = {
    "domain": Keep(),
    "mode": Keep(),
    "status": Keep(),
    "tool_tags": Keep(),
    "domain_confidence": Keep(),
    "reason": Keep(),
    "execution_domain": Keep(),
    "domain_tasks": Nested({"domain": Keep(), "status": Keep()}),
}

ALLOWLIST: Mapping[str, Mapping[str, Rule]] = {
    "request-orchestrator@v1": {
        "runtime": Nested(_RUNTIME),
        "use_online_agent": Keep(),
        "domain_lock": Keep(),
        "itinerary_mode": Keep(),
    },
    "task-graph@v1": {
        "runtime": Nested(_RUNTIME),
    },
    "routing-graph@v1": {
        "intent": Keep(),
        "domain": Keep(),
        "domain_confidence": Keep(),
        "domain_reason": Keep(),
        "mode": Keep(),
        "domain_lock": Keep(),
        "force_itinerary": Keep(),
        "route": Nested(_DOMAIN_ROUTE),
        "retry_count": Keep(),
        "max_retries": Keep(),
        "detected_domains": Keep(),
    },
    "generic-domain@v1": {
        "domain": Keep(),
        "use_online_agent": Keep(),
        "tool_tags": Keep(),
        "selected_tools": Keep(),
        "skipped_tools": Keep(),
        "executions": Nested({"tool": Keep(), "count": Keep()}),
        "items": CountOnly(),
        "progress_events": Nested({"stage": Keep(), "status": Keep(), "progress": Keep()}),
        "gather_iterations": Keep(),
        "candidate_count": Keep(),
    },
    "restaurant-domain@v1": {
        "use_online_agent": Keep(),
        "tool_tags": Keep(),
        "plan_calls": Nested({"tool": Keep()}),
        "selected_tools": Keep(),
        "skipped_tools": Keep(),
        "executions": Nested({"tool": Keep(), "count": Keep()}),
        "restaurants": CountOnly(),
        "checked_restaurants": CountOnly(),
        "rejection_stats": Keep(),
        "refine_used": Keep(),
        "food_intent_widened": Keep(),
        "progress_events": Nested({"stage": Keep(), "status": Keep(), "progress": Keep()}),
    },
}


# ---------------------------------------------------------------------------
# Denied keys and value patterns
# ---------------------------------------------------------------------------

#: Never stored, at any depth, however the allowlist is edited later.
DENIED_KEYS = frozenset({
    "user_id", "conversation_id", "thread_id", "task_thread_id",
    "query", "user_input", "preferences", "profile_updates", "user_profile",
    "conversation_history", "restaurant_baseline", "collect_confirm_state",
    "domain_graph_result", "response_payload", "metadata",
    "message", "reply", "summary_content", "execution_data",
    "result", "results", "items_detail", "payload",
    "email", "phone", "address", "postal_code", "nric", "name", "username",
    "password", "token", "api_key", "secret", "authorization",
})

#: A kept value that matches one of these is dropped whatever it is called.
_VALUE_PATTERNS: Sequence[Tuple[str, re.Pattern[str]]] = (
    ("email_pattern", re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")),
    ("nric_pattern", re.compile(r"\b[STFGM]\d{7}[A-Za-z]\b")),
    # 8+ digits in a row, optionally spaced or with a country code. A local
    # Singapore number is 8 digits with no prefix, so the floor is 8, not 9.
    ("phone_pattern", re.compile(r"(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){7,}\d")),
    ("card_pattern", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    # "Bearer <token>" carries a space; the vendor prefixes do not.
    ("bearer_pattern", re.compile(
        r"\b(?:Bearer\s+[A-Za-z0-9._-]{8,}|(?:sk-|ghp_|gho_|AIza|xox[baprs]-)[A-Za-z0-9._-]{8,})")),
    ("url_with_query_pattern", re.compile(r"https?://\S+\?\S+")),
)

# Reason codes. Each matches Omission's ^[a-z][a-z0-9_]{0,63}$.
REASON_NOT_ALLOWLISTED = "not_allowlisted"
REASON_DENIED_KEY = "denied_key"
REASON_UNSUPPORTED_TYPE = "unsupported_type"
REASON_TOO_DEEP = "nesting_depth_exceeded"
REASON_TOO_MANY_ITEMS = "collection_items_exceeded"


@dataclass(frozen=True)
class Bounds:
    max_string_chars: int = MAX_STRING_CHARS
    max_collection_items: int = MAX_COLLECTION_ITEMS
    max_nesting_depth: int = MAX_NESTING_DEPTH


# ---------------------------------------------------------------------------
# Safe errors (DECISIONS.md R10)
# ---------------------------------------------------------------------------

#: code -> (safe message, retryable). The message is fixed: it never
#: interpolates a value, so an error can never become an exfiltration path.
#: `retryable` is a property of the code, not of the instance.
ERROR_CODES: Mapping[str, Tuple[str, bool]] = {
    "cancelled": ("Run was cancelled", False),
    "execution_failed": ("Run failed", False),
    "timeout": ("Run exceeded its time budget", True),
    "validation_failed": ("A value did not satisfy its contract", False),
    "provider_unavailable": ("An upstream provider was unavailable", True),
    "quota_exceeded": ("An upstream quota was exhausted", True),
    "not_allowlisted": ("A value was withheld by policy", False),
}

_ERROR_CLASS_TO_CODE: Mapping[str, str] = {
    "CancelledError": "cancelled",
    "TimeoutError": "timeout",
    "ValidationError": "validation_failed",
    "ValueError": "validation_failed",
    "ConnectionError": "provider_unavailable",
    "ConnectTimeout": "provider_unavailable",
    "ServiceUnavailable": "provider_unavailable",
    "RateLimitError": "quota_exceeded",
}


def safe_error(error: BaseException) -> SafeError:
    """Classify an exception. The exception's own message never reaches a trace."""
    name = type(error).__name__
    if isinstance(error, asyncio.CancelledError):
        code = "cancelled"
    elif isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        code = "timeout"
    else:
        code = _ERROR_CLASS_TO_CODE.get(name, "execution_failed")
    message, retryable = ERROR_CODES[code]
    return SafeError(
        code=code,
        error_class=_safe_class_name(name),
        safe_message=message,
        retryable=retryable,
    )


def _safe_class_name(name: str) -> str:
    """An exception class name, reduced to the SafeId character set."""
    cleaned = re.sub(r"[^A-Za-z0-9._:@/-]", "", name)[:128]
    return cleaned or "Exception"


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

class _Run:
    """One pass of the pipeline. Collects omissions and truncations as it goes."""

    def __init__(self, bounds: Bounds) -> None:
        self.bounds = bounds
        self.omissions: list[Omission] = []
        self.truncated: list[str] = []

    def omit(self, path: str, reason: str) -> None:
        if len(self.omissions) < MAX_COLLECTION_ITEMS:
            self.omissions.append(Omission(path=_safe_path(path), reason=reason))

    def truncate(self, path: str) -> None:
        if len(self.truncated) < MAX_COLLECTION_ITEMS:
            self.truncated.append(_safe_path(path))

    # -- stage 1: allowlist ------------------------------------------------
    def apply(self, value: Any, rules: Mapping[str, Rule], path: str, depth: int) -> Dict[str, Any]:
        """Keep only the named fields of a mapping, recursing where told to."""
        if not isinstance(value, Mapping):
            value = _as_mapping(value)
        if value is None:
            return {}
        kept: Dict[str, Any] = {}
        for key, raw in value.items():
            child = f"{path}.{key}" if path else str(key)
            if not isinstance(key, str):
                self.omit(child, REASON_UNSUPPORTED_TYPE)
                continue
            rule = rules.get(key)
            if rule is None:
                self.omit(child, REASON_NOT_ALLOWLISTED)
                continue
            # stage 2 runs even on an allowlisted name, so a denied name added
            # inside an allowlisted object later is still refused.
            if key in DENIED_KEYS:
                self.omit(child, REASON_DENIED_KEY)
                continue
            if raw is None:
                kept[key] = None
            elif isinstance(rule, CountOnly):
                kept[key] = _count_of(raw)
            elif isinstance(rule, Nested):
                kept[key] = self._nested(raw, rule.fields, child, depth + 1)
            else:
                bounded = self.bound(raw, child, depth + 1)
                if bounded is not _DROP:
                    kept[key] = bounded
        return kept

    def _nested(self, raw: Any, rules: Mapping[str, Rule], path: str, depth: int) -> Any:
        if depth >= self.bounds.max_nesting_depth:
            self.omit(path, REASON_TOO_DEEP)
            return None
        if isinstance(raw, (list, tuple)):
            items = list(raw)
            if len(items) > self.bounds.max_collection_items:
                self.omit(path, REASON_TOO_MANY_ITEMS)
                items = items[: self.bounds.max_collection_items]
                self.truncate(path)
            return [self.apply(item, rules, f"{path}[{i}]", depth + 1)
                    for i, item in enumerate(items)]
        return self.apply(raw, rules, path, depth)

    # -- stages 3 and 4: patterns and bounds -------------------------------
    def bound(self, value: Any, path: str, depth: int) -> Any:
        """Enforce the safe-value grammar on an allowlisted value."""
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                self.omit(path, REASON_UNSUPPORTED_TYPE)
                return _DROP
            return value
        if isinstance(value, str):
            reason = _matched_pattern(value)
            if reason is not None:
                self.omit(path, reason)
                return _DROP
            if len(value) > self.bounds.max_string_chars:
                self.truncate(path)
                return value[: self.bounds.max_string_chars]
            return value
        if depth >= self.bounds.max_nesting_depth:
            self.omit(path, REASON_TOO_DEEP)
            return _DROP
        if isinstance(value, (list, tuple)):
            items = list(value)
            if len(items) > self.bounds.max_collection_items:
                self.truncate(path)
                items = items[: self.bounds.max_collection_items]
            out = []
            for index, item in enumerate(items):
                bounded = self.bound(item, f"{path}[{index}]", depth + 1)
                if bounded is not _DROP:
                    out.append(bounded)
            return out
        if isinstance(value, Mapping):
            pairs = list(value.items())
            if len(pairs) > self.bounds.max_collection_items:
                self.truncate(path)
                pairs = pairs[: self.bounds.max_collection_items]
            out_map: Dict[str, Any] = {}
            for key, item in pairs:
                child = f"{path}.{key}"
                if not isinstance(key, str) or len(key) > self.bounds.max_string_chars:
                    self.omit(child, REASON_UNSUPPORTED_TYPE)
                    continue
                if key in DENIED_KEYS:
                    self.omit(child, REASON_DENIED_KEY)
                    continue
                bounded = self.bound(item, child, depth + 1)
                if bounded is not _DROP:
                    out_map[key] = bounded
            return out_map
        mapped = _as_mapping(value)
        if mapped is not None:
            return self.bound(mapped, path, depth)
        self.omit(path, REASON_UNSUPPORTED_TYPE)
        return _DROP


class _Drop:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<dropped>"


_DROP = _Drop()


def _matched_pattern(value: str) -> Optional[str]:
    for reason, pattern in _VALUE_PATTERNS:
        if pattern.search(value):
            return reason
    return None


def _as_mapping(value: Any) -> Optional[Mapping[str, Any]]:
    """Pydantic models and dataclass-like objects, as plain mappings."""
    for attr in ("model_dump", "dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                dumped = method()
            except TypeError:
                continue
            if isinstance(dumped, Mapping):
                return dumped
    if hasattr(value, "__dict__") and not isinstance(value, type):
        contents = vars(value)
        if contents:
            return contents
    return None


def _count_of(value: Any) -> Optional[int]:
    try:
        return len(value)
    except TypeError:
        return None


def _safe_path(path: str) -> str:
    return (path or "(root)")[:MAX_STRING_CHARS]


def _rules_for(graph_definition_id: Optional[str]) -> Optional[Mapping[str, Rule]]:
    if graph_definition_id is None:
        return None
    return ALLOWLIST.get(graph_definition_id)


# ---------------------------------------------------------------------------
# Summaries and diffs
# ---------------------------------------------------------------------------

def _summary(kept: Mapping[str, Any], omitted_count: int) -> PayloadSummary:
    """Describe a payload without carrying it: names and sizes, not values."""
    keys = sorted(str(k)[:MAX_STRING_CHARS] for k in kept)[:MAX_COLLECTION_ITEMS]
    return PayloadSummary(
        keys=keys,
        bytes=len(json.dumps(kept, default=str).encode("utf-8")),
        value_types={k: type(kept[k]).__name__ for k in keys},
        item_count=len(kept),
        omitted_count=omitted_count,
    )


def _diff(before: Mapping[str, Any], after: Mapping[str, Any],
          omissions: Sequence[Omission]) -> StateDiff:
    """Compare a node's allowlisted input with its allowlisted returned update.

    `removed` holds only explicit deletions: a key the update set to null that
    had a value before. A key the update simply did not mention is not a
    deletion, it is an absence of news (DECISIONS.md R3).
    """
    added: Dict[str, Any] = {}
    changed: Dict[str, ChangedValue] = {}
    removed: list[str] = []
    for key, new in after.items():
        if key not in before:
            if new is not None:
                added[key] = new
            continue
        old = before[key]
        if new is None and old is not None:
            removed.append(key)
        elif old != new:
            changed[key] = ChangedValue(before=old, after=new)
    return StateDiff(
        basis="node_input_to_update",
        added=dict(list(added.items())[:MAX_COLLECTION_ITEMS]),
        changed=dict(list(changed.items())[:MAX_COLLECTION_ITEMS]),
        removed=removed[:MAX_COLLECTION_ITEMS],
        omitted=list(omissions)[:MAX_COLLECTION_ITEMS],
    )


# ---------------------------------------------------------------------------
# The projector
# ---------------------------------------------------------------------------

_CHILD_SPANS = {"tool_start", "tool_end", "llm_start", "llm_end",
                "policy_start", "policy_end"}


class SafeProjectorV1:
    """``metarec-debug-safe-v1``. Implements ``interfaces.SafeProjector``.

    One call per event. Raw values never leave this object: the collector gets
    summaries, a diff of allowlisted fields, a classified error and the paths
    that were withheld.
    """

    def __init__(self, bounds: Optional[Bounds] = None, policy_id: str = POLICY_ID) -> None:
        self.policy_id = policy_id
        self.bounds = bounds or Bounds()

    def project(
        self,
        *,
        event_type: str,
        runnable_type: str,
        graph_definition_id: str,
        node_id: Optional[str] = None,
        node_input: Any = None,
        output: Any = None,
        error: Optional[BaseException] = None,
        raw_name: Optional[str] = None,
    ) -> Projection:
        run = _Run(self.bounds)
        rules = _rules_for(graph_definition_id)

        # A child span carries a name and nothing else. Prompts, tool
        # arguments and tool results never reach the projector, and would be
        # refused here if they did.
        if event_type in _CHILD_SPANS:
            return Projection(
                metadata=_child_metadata(event_type, raw_name),
                error=safe_error(error) if error is not None else None,
            )

        if rules is None:
            # An unregistered graph. Nothing is known to be safe in a state
            # this module has never read, so nothing is kept.
            if node_input is not None or output is not None:
                run.omit("(root)", REASON_NOT_ALLOWLISTED)
            return Projection(
                error=safe_error(error) if error is not None else None,
                redacted_paths=tuple(o.path for o in run.omissions),
            )

        input_summary = output_summary = None
        state_diff = None

        kept_input: Dict[str, Any] = {}
        if node_input is not None:
            before = len(run.omissions)
            kept_input = run.apply(node_input, rules, "", 0)
            input_summary = _summary(kept_input, len(run.omissions) - before)

        kept_output: Dict[str, Any] = {}
        if output is not None:
            before = len(run.omissions)
            kept_output = run.apply(output, rules, "", 0)
            output_summary = _summary(kept_output, len(run.omissions) - before)

        # R3: a diff exists only on node_end, where there is a returned update.
        if event_type == "node_end" and output is not None:
            state_diff = _diff(kept_input, kept_output, run.omissions)

        return Projection(
            input_summary=input_summary,
            output_summary=output_summary,
            state_diff=state_diff,
            metadata=None,
            error=safe_error(error) if error is not None else None,
            redacted_paths=tuple(o.path for o in run.omissions),
            truncated_paths=tuple(run.truncated),
        )


def _child_metadata(event_type: str, raw_name: Optional[str]) -> Optional[EventMetadata]:
    if not raw_name:
        return None
    name = _safe_class_name(raw_name)
    if not name:
        return None
    if event_type.startswith("tool"):
        return EventMetadata(tool=name)
    if event_type.startswith("llm"):
        return EventMetadata(model=name)
    return None
