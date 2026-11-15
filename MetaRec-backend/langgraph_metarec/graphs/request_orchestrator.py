from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from langgraph_metarec.checkpointing import RuntimeCheckpointer, conversation_thread_id
from langgraph_metarec.graphs.routing_graph import (
    DomainRoute,
    _preference_domain_hint,
    run_routing_graph,
    supported_domains_phrase,
    tool_tags_for_domain,
)
from langgraph_metarec.nodes.domain import classify_domain
from langgraph_metarec.nodes.preferences import build_collect_confirm_state_payload, merge_preferences
from langgraph_metarec.state import (
    GraphRuntimeState,
    IntentResult,
    RuntimeErrorRecord,
    TaskStatusProjection,
)
from langgraph_metarec.wp8_trace.invocation import TraceContext, invoke_with_wp8_trace, trace_kwargs


AnalyzeMessage = Callable[..., Awaitable[Any]]
ConfirmationFactory = Callable[..., Awaitable[Dict[str, Any]]]
TaskFactory = Callable[[str, Dict[str, Any], Optional[List[str]], Optional[Dict[str, Any]]], Awaitable[str]]
PreferenceExtractor = Callable[[str], Dict[str, Any]]
AnchorCandidateResolver = Callable[[str, str, Optional[str]], Awaitable[List[Dict[str, Any]]]]


@dataclass
class RequestOrchestratorAdapters:
    analyze_message: AnalyzeMessage
    make_confirmation: ConfirmationFactory
    create_task: TaskFactory
    extract_preferences: PreferenceExtractor
    extract_itinerary_constraints: Optional[Callable[[str, Optional[Dict[str, Any]]], Awaitable[Optional[Dict[str, Any]]]]] = None
    resolve_itinerary_anchor: Optional[AnchorCandidateResolver] = None


class RequestOrchestratorState(TypedDict, total=False):
    runtime: Dict[str, Any]
    conversation_history: List[Dict[str, Any]]
    user_profile: Optional[Dict[str, Any]]
    # Restaurant-only runtime preference baseline (defaults + profile + conversation).
    # Used solely as the merge base for the *restaurant* refine/dispatch paths; other
    # domains never inherit it (see _refine_preferences).
    restaurant_baseline: Optional[Dict[str, Any]]
    use_online_agent: bool
    domain_lock: Optional[str]
    itinerary_mode: bool


def _route_to_dict(route: Optional[DomainRoute], domain_lock: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if route is None:
        return None
    return route.to_payload(domain_lock)


def _confirmation_request_from_hitl(hitl_state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(hitl_state, dict):
        return None
    confirmation = hitl_state.get("confirmation_request")
    return confirmation if isinstance(confirmation, dict) else None


def _confirmation_message_from_hitl(hitl_state: Optional[Dict[str, Any]]) -> Optional[str]:
    confirmation = _confirmation_request_from_hitl(hitl_state)
    if confirmation:
        message = confirmation.get("message")
        if message:
            return str(message)
    return None


def _modification_confirmation(preferences: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "message": "No problem. Update the preferences below, then confirm to continue.",
        "preferences": preferences,
        "needs_confirmation": True,
    }


# Restaurant-ONLY preference keys, stripped before preferences reach a generic
# domain so restaurant defaults never reshape a movie/music/book/product/hotel
# request. `location` is deliberately NOT in this set: it is a genuinely generic
# key (hotels anchor on it, and the leakage concern was restaurant *defaults*,
# which `_generic_preference_subset`'s fresh-extraction inputs never carry).
_RESTAURANT_PREFERENCE_KEYS = {
    "restaurant_types",
    "flavor_profiles",
    "dining_purpose",
    "food_intent",
}


def _is_meaningful_budget_range(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    budget_min = value.get("min")
    budget_max = value.get("max")
    if budget_min in (None, "") and budget_max in (None, ""):
        return False
    # Common restaurant default emitted by older prompts; do not let it leak
    # into generic domains unless the user actually supplied a different budget.
    return (budget_min, budget_max) not in {(20, 60), ("20", "60")}


def _generic_preference_subset(preferences: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(preferences, dict):
        return {}
    result: Dict[str, Any] = {}
    for key, value in preferences.items():
        if key in _RESTAURANT_PREFERENCE_KEYS or value in (None, "", [], {}):
            continue
        if key == "budget_range" and not _is_meaningful_budget_range(value):
            continue
        result[key] = value
    return result


def _budget_text_from_range(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    currency = str(value.get("currency") or "SGD").upper()
    budget_min = value.get("min")
    budget_max = value.get("max")
    if budget_min not in (None, "") and budget_max not in (None, ""):
        return f"{budget_min}-{budget_max} {currency}"
    if budget_max not in (None, ""):
        return f"<= {budget_max} {currency}"
    if budget_min not in (None, ""):
        return f">= {budget_min} {currency}"
    return ""


_BUDGET_AMOUNT_RE = re.compile(
    r"(?P<prefix><=|<|under|below|以内|以下|不超过|少于|低于)?\s*"
    r"(?P<sym>[$￥¥])?\s*(?P<amount>\d+(?:[,.]\d+)?)\s*"
    r"(?P<currency>SGD|USD|CNY|RMB|EUR|新币|美元|人民币)?",
    re.IGNORECASE,
)
_BUDGET_UPPER_RE = re.compile(r"(以内|以下|不超过|少于|低于|under|below)", re.IGNORECASE)
_CURRENCY_NORMALIZE = {"新币": "SGD", "美元": "USD", "人民币": "CNY", "RMB": "CNY"}


def _extract_product_budget_text(query: str, preferences: Dict[str, Any]) -> str:
    explicit = preferences.get("budget")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    from_range = _budget_text_from_range(preferences.get("budget_range"))
    if from_range:
        return from_range
    text = query or ""
    # A number only counts as a budget when it carries a currency token/symbol or
    # sits next to a budget keyword. Bare numbers (e.g. the "14" in "iPhone 14") are
    # ignored, so a model/version number is never misread as a price.
    for match in _BUDGET_AMOUNT_RE.finditer(text):
        sym = match.group("sym")
        currency_tok = match.group("currency")
        prefix = match.group("prefix")
        trailing = text[match.end():match.end() + 3]
        has_currency = bool(sym or currency_tok)
        has_keyword = bool(prefix) or bool(re.match(r"\s*(以内|以下)", trailing))
        if not (has_currency or has_keyword):
            continue
        amount = match.group("amount").replace(",", "")
        if currency_tok:
            currency = _CURRENCY_NORMALIZE.get(currency_tok, currency_tok.upper())
        elif sym in ("￥", "¥"):
            currency = "CNY"
        else:
            currency = "SGD"  # "$" defaults to SGD to match the app's SGD-centric defaults
        is_upper = bool(prefix) or bool(_BUDGET_UPPER_RE.search(text))
        return f"<= {amount} {currency}" if is_upper else f"{amount} {currency}"
    if re.search(r"(不那么贵|便宜|实惠|预算有限|affordable|cheap|budget)", text, re.IGNORECASE):
        return "affordable"
    return ""


def _normalize_product_preferences(preferences: Dict[str, Any], query: str) -> Dict[str, Any]:
    result = dict(preferences or {})
    text = " ".join(str(part or "") for part in [query, result.get("query")]).strip()
    lower = text.lower()

    product = str(result.get("product") or result.get("item") or "").strip()
    brand = str(result.get("brand") or "").strip()
    category = str(result.get("category") or "").strip()
    use_case = str(result.get("use_case") or "").strip()

    if not product:
        if re.search(r"(iphone|ios|苹果手机|蘋果手機)", lower, re.IGNORECASE):
            product = "iPhone"
        elif re.search(r"(ipad|平板)", lower, re.IGNORECASE):
            product = "iPad"
        elif re.search(r"(laptop|notebook|电脑|筆電|笔记本|筆記本)", lower, re.IGNORECASE):
            product = "laptop"
        elif re.search(r"(headphones?|earbuds?|耳机|耳機)", lower, re.IGNORECASE):
            product = "headphones"
        # \bphones?\b so the substring "phone" in "headphones" is not matched.
        elif re.search(r"(\bphones?\b|smartphone|手机|手機)", lower, re.IGNORECASE):
            product = "smartphone"
    if product:
        result["product"] = product

    if not brand and re.search(r"(iphone|ipad|ios|apple|苹果|蘋果)", lower, re.IGNORECASE):
        result["brand"] = "Apple"
    elif brand:
        result["brand"] = brand

    if not category:
        if re.search(r"(iphone|ios|\bphones?\b|smartphone|手机|手機)", lower, re.IGNORECASE):
            category = "smartphone"
        elif re.search(r"(laptop|notebook|电脑|筆電|笔记本|筆記本)", lower, re.IGNORECASE):
            category = "laptop"
        elif product:
            category = product
    if category:
        result["category"] = category

    if not use_case:
        if re.search(r"(ios|app).*(test|测试|測試)|测试.*ios|測試.*ios", lower, re.IGNORECASE):
            use_case = "iOS testing"
        elif re.search(r"(办公|工作|office|work)", lower, re.IGNORECASE):
            use_case = "work"
        elif re.search(r"(学习|學習|study|school)", lower, re.IGNORECASE):
            use_case = "study"
        elif re.search(r"(游戏|遊戲|gaming|game)", lower, re.IGNORECASE):
            use_case = "gaming"
    if use_case:
        result["use_case"] = use_case

    model = str(result.get("model") or "").strip()
    if not model:
        model_match = re.search(r"(iphone|ipad)\s*(\d{1,2})(?:\s*(?:-|~|到|至)\s*(\d{1,2}))?", text, re.IGNORECASE)
        if model_match:
            start = model_match.group(2)
            end = model_match.group(3)
            model = f"{model_match.group(1).title()} {start}-{end}" if end else f"{model_match.group(1).title()} {start}"
    if model:
        result["model"] = model

    budget = _extract_product_budget_text(text, result)
    if budget:
        result["budget"] = budget
    return result


def _normalize_domain_preferences(domain: str, preferences: Dict[str, Any], query: str) -> Dict[str, Any]:
    if str(domain or "").lower() == "product":
        return _normalize_product_preferences(preferences, query)
    return preferences


def _enrich_hotel_preferences_from_profile(
    preferences: Dict[str, Any],
    user_profile: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not isinstance(preferences, dict) or str(preferences.get("domain") or "").lower() != "hotel":
        return preferences
    try:
        from profile_model import enrich_hotel_location_preferences

        return enrich_hotel_location_preferences(preferences, user_profile)
    except Exception:
        return preferences


def _hotel_location_needs_clarification(
    preferences: Dict[str, Any],
    user_profile: Optional[Dict[str, Any]],
) -> bool:
    if not isinstance(preferences, dict) or str(preferences.get("domain") or "").lower() != "hotel":
        return False
    try:
        from profile_model import hotel_location_needs_clarification

        return hotel_location_needs_clarification(preferences, user_profile)
    except Exception:
        return False


def _hotel_location_clarification(preferences: Dict[str, Any]) -> Dict[str, Any]:
    location = str((preferences or {}).get("location") or "").strip()
    if location and location.lower() != "any":
        message = (
            f"I need a more specific hotel destination for '{location}'. "
            "Please add the city or country, then confirm to continue."
        )
    else:
        message = "Please add the hotel destination or area, then confirm to continue."
    return {
        "message": message,
        "preferences": preferences,
        "needs_confirmation": True,
    }


def _merge_generic_preferences(
    base: Optional[Dict[str, Any]],
    overlay: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    result = _generic_preference_subset(base)
    result.update(_generic_preference_subset(overlay))
    return result


def _refine_preferences(
    *,
    routing: Optional[Dict[str, Any]],
    previous: Optional[Dict[str, Any]],
    new: Optional[Dict[str, Any]],
    restaurant_baseline: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Domain-aware merge for refining an open confirmation (shared by the
    reject-button and text-rejection paths).

    Restaurant folds the new preferences onto its rich runtime baseline. Every
    other domain merges only generic keys onto the set already under review and
    **never** inherits the restaurant baseline, so a movie/music/book/product
    refinement keeps its structured prefs instead of being reshaped into a
    restaurant request.
    """
    if _route_domain(routing) == "restaurant":
        base = previous or restaurant_baseline or {}
        return merge_preferences(base, new)
    return _merge_generic_preferences(previous or {}, new)


def _preference_domain(preferences: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(preferences, dict):
        return None
    value = str(preferences.get("domain") or "").strip().lower()
    return value or None


def _classified_query_domain(query: str) -> Optional[str]:
    domain, _, _ = classify_domain(query or "")
    return None if domain == "unknown" else domain


def _starts_new_query_flow(
    collect_state: Dict[str, Any],
    query: str,
    preferences: Optional[Dict[str, Any]],
) -> bool:
    previous_domain = _route_domain(collect_state.get("routing"))
    query_domain = _classified_query_domain(query)
    if query_domain:
        return True
    # Semantic evidence from the LLM-structured preferences — explicit ``domain`` or
    # entity keys like artist/director/author. Reusing the same hint routing uses
    # lets the in-flow guard recognize a domain switch even when the query carries
    # no domain keyword (e.g. "by Nolan" while a music confirmation is open).
    hint = _preference_domain_hint(preferences)
    pref_domain = (hint[0] if hint else None) or _preference_domain(preferences)
    if not pref_domain:
        return False
    if previous_domain in {"recommendation", "unknown"}:
        return True
    return pref_domain != previous_domain


def _route_domain(route: Optional[Dict[str, Any]]) -> str:
    if not isinstance(route, dict):
        return "recommendation"
    return str(route.get("execution_domain") or route.get("domain") or "recommendation")


def _unsupported_domain_reply(domain: Optional[str]) -> str:
    """Graceful, extendable reply for a query we can't serve — an unknown/ambiguous
    domain or a recognized-but-not-connected one. Always points the user at the
    currently supported domains (single source: routing.supported_domains_phrase)."""
    phrase = supported_domains_phrase()
    domain_key = str(domain or "").lower()
    if domain_key and domain_key not in {"unknown", "multi_domain", "recommendation"}:
        return (
            f"It looks like you're after {domain_key} recommendations, which I don't "
            f"support yet. I can help with {phrase} — feel free to ask about any of those!"
        )
    return (
        "I'm not sure what kind of recommendation you're after. "
        f"I can help with {phrase} — feel free to ask about any of those!"
    )


def _list_phrase(names: List[str]) -> str:
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


_LODGING_MODES = {"none", "supplied", "recommend"}
_CLEARABLE_HITL_PREFERENCE_KEYS = frozenset({"hotel_anchor", "end_anchor", "anchor_policy"})


def _itinerary_lodging_mode(route: Optional[Dict[str, Any]], preferences: Dict[str, Any]) -> str:
    """Return the explicit lodging intent without treating every trip as anchored."""
    configured = str((preferences or {}).get("lodging_mode") or "").strip().lower()
    hotel_supplied = bool(str((preferences or {}).get("hotel_anchor") or "").strip())
    hotel_requested = bool(
        isinstance(route, dict) and (route.get("metadata") or {}).get("hotel_anchor_requested")
    )
    try:
        is_multi_day = int((preferences or {}).get("horizon_days") or 1) > 1
    except (TypeError, ValueError):
        is_multi_day = False

    if hotel_supplied or configured == "supplied":
        return "supplied"
    if configured == "none":
        return "recommend" if is_multi_day else "none"
    if hotel_requested:
        return "supplied"
    # A multi-day itinerary always has an overnight lodging requirement. LLM
    # extraction and older checkpoints may carry `none` when no hotel was named;
    # that means the planner should recommend one shared hotel, not omit lodging.
    if is_multi_day:
        return "recommend"
    if configured in _LODGING_MODES:
        return configured
    return "none"


def _with_itinerary_lodging_mode(
    route: Optional[Dict[str, Any]],
    preferences: Dict[str, Any],
) -> Dict[str, Any]:
    enriched = dict(preferences or {})
    enriched["lodging_mode"] = _itinerary_lodging_mode(route, enriched)
    return enriched


def _apply_preference_clears(
    preferences: Dict[str, Any],
    clear_fields: Any,
) -> Dict[str, Any]:
    """Apply explicit HITL clears while ignoring untrusted or unrelated keys."""
    result = dict(preferences or {})
    requested = {
        str(key)
        for key in (clear_fields if isinstance(clear_fields, (list, tuple, set)) else [])
        if str(key) in _CLEARABLE_HITL_PREFERENCE_KEYS
    }
    if not requested:
        return result
    sources = result.get("_itinerary_field_sources")
    sources = dict(sources) if isinstance(sources, dict) else {}
    resolved = result.get("resolved_anchors")
    resolved = dict(resolved) if isinstance(resolved, dict) else {}
    attempts = result.get("_anchor_resolution_attempts")
    attempts = dict(attempts) if isinstance(attempts, dict) else {}
    for key in requested:
        result.pop(key, None)
        sources.pop(key, None)
        if key == "hotel_anchor":
            resolved.pop("start", None)
            attempts.pop("start", None)
            if str(result.get("lodging_mode") or "").lower() != "none":
                result.pop("lodging_mode", None)
        elif key == "end_anchor":
            resolved.pop("end", None)
            attempts.pop("end", None)
    result.pop("_anchor_clarification", None)
    result.pop("_anchor_resolution_error", None)
    result["resolved_anchors"] = resolved
    result["_anchor_resolution_attempts"] = attempts
    result["_itinerary_field_sources"] = sources
    return result


def _merge_hitl_preferences(
    current: Dict[str, Any],
    incoming: Any,
    clear_fields: Any,
) -> Dict[str, Any]:
    """Merge a client overlay without allowing stale values to undo clears."""
    cleared = _apply_preference_clears(current, clear_fields)
    overlay = _meaningful_preference_overlay(incoming)
    explicit_clears = {
        str(key)
        for key in (clear_fields if isinstance(clear_fields, (list, tuple, set)) else [])
        if str(key) in _CLEARABLE_HITL_PREFERENCE_KEYS
    }
    for key in explicit_clears:
        overlay.pop(key, None)
    return {**cleared, **overlay}


def _itinerary_preference_form(route: Dict[str, Any], preferences: Dict[str, Any]) -> Dict[str, Any]:
    from preference_specs import build_domain_form

    form_preferences = dict(preferences or {})
    form_preferences.setdefault("horizon_days", 1)
    if not form_preferences.get("daily_start_time") and form_preferences.get("start_time"):
        form_preferences["daily_start_time"] = form_preferences["start_time"]
    if not form_preferences.get("daily_end_time") and form_preferences.get("end_time"):
        form_preferences["daily_end_time"] = form_preferences["end_time"]
    form = build_domain_form("itinerary", form_preferences)
    if _itinerary_lodging_mode(route, preferences) == "supplied":
        for field in form.get("fields") or []:
            if isinstance(field, dict) and field.get("key") == "hotel_anchor":
                field["required"] = True
        if not str(preferences.get("hotel_anchor") or "").strip():
            missing = list(form.get("missing_required") or [])
            if "hotel_anchor" not in missing:
                missing.append("hotel_anchor")
            form["missing_required"] = missing
            form["complete"] = False
    return form


def _itinerary_confirmation(query: str, route: Dict[str, Any], preferences: Dict[str, Any]) -> Dict[str, Any]:
    """Confirm explicit planning constraints, never an invented slot skeleton."""
    form = _itinerary_preference_form(route, preferences)
    location = str((preferences or {}).get("location") or "").strip()
    try:
        horizon_days = int((preferences or {}).get("horizon_days") or 1)
    except (TypeError, ValueError):
        horizon_days = 1
    if not 1 <= horizon_days <= 3:
        message = (
            "Dynamic itinerary planning supports one to three consecutive days. "
            "Choose a trip length within that range below."
        )
    else:
        # Round 1 shows no form, so a confirm-immediately user only ever sees the
        # trip framing in this message. Mirror planning_request_from_preferences
        # exactly: when the user named no date/window we still plan (tomorrow,
        # 09:00-22:00), so surface those effective values here — labelled
        # "(default ...)" — instead of silently omitting them. Same source of truth
        # means the message can never disagree with what actually gets planned.
        from langgraph_metarec.itinerary_contracts import (
            DEFAULT_DAILY_END_MIN,
            DEFAULT_DAILY_START_MIN,
            _default_first_date,
        )

        date = str(preferences.get("date") or "").strip()
        start = str(preferences.get("daily_start_time") or preferences.get("start_time") or "").strip()
        end = str(preferences.get("daily_end_time") or preferences.get("end_time") or "").strip()
        date_is_default = not date
        if date_is_default:
            date = _default_first_date(preferences.get("timezone")).isoformat()
        start_is_default = not start
        if start_is_default:
            start = f"{DEFAULT_DAILY_START_MIN // 60:02d}:{DEFAULT_DAILY_START_MIN % 60:02d}"
        end_is_default = not end
        if end_is_default:
            end = f"{DEFAULT_DAILY_END_MIN // 60:02d}:{DEFAULT_DAILY_END_MIN % 60:02d}"
        pace = str(preferences.get("pace") or "").strip()
        style = str(preferences.get("style") or "").strip()
        timezone = str(preferences.get("timezone") or "").strip()
        if preferences.get("budget_mode") == "unlimited":
            budget = "no budget limit"
        elif (
            preferences.get("budget_mode") == "limited"
            and preferences.get("budget_amount") not in (None, "")
            and str(preferences.get("budget_currency") or "").strip()
        ):
            currency = str(preferences.get("budget_currency") or "").strip()
            amount = f"{preferences.get('budget_amount')} {currency}".strip()
            budget = f"a total trip budget of {amount} per person"
        else:
            budget = ""
        descriptors = " ".join(part for part in (pace, style) if part)
        summary = (
            f"I'll dynamically plan a {descriptors} itinerary"
            if descriptors
            else "I'll dynamically plan an itinerary"
        )
        if location:
            summary += f" around {location}"
        if horizon_days == 1:
            summary += f" on {date}" + (" (default date)" if date_is_default else "")
        else:
            try:
                last_date = (_dt.date.fromisoformat(date) + _dt.timedelta(days=horizon_days - 1)).isoformat()
                summary += f" from {date} through {last_date} ({horizon_days} days)" + (
                    " (default start date)" if date_is_default else ""
                )
            except ValueError:
                summary += f" for {horizon_days} days from {date}"
        prefix = "daily " if horizon_days > 1 else ""
        if start_is_default and end_is_default:
            window_note = " (default hours)"
        elif start_is_default:
            window_note = " (default start time)"
        elif end_is_default:
            window_note = " (default end time)"
        else:
            window_note = ""
        summary += f", {prefix}from {start} to {end}{window_note}"
        if budget:
            summary += f", with {budget}"
        if timezone:
            summary += f", timezone {timezone}"
        interests = preferences.get("interest_terms") or preferences.get("attraction_types") or []
        if isinstance(interests, str):
            interests = [part.strip() for part in interests.split(",") if part.strip()]
        if isinstance(interests, (list, tuple, set)) and interests:
            rendered_interests = _list_phrase([
                str(value).replace("-", " ") for value in interests if str(value).strip()
            ])
            if rendered_interests:
                summary += f", focused on {rendered_interests}"
        hotel_anchor = str(preferences.get("hotel_anchor") or "").strip()
        lodging_mode = _itinerary_lodging_mode(route, preferences)
        anchor_policy = str(preferences.get("anchor_policy") or "").strip()
        end_anchor = str(preferences.get("end_anchor") or "").strip()
        if horizon_days > 1 and lodging_mode == "recommend":
            summary += f", selecting one shared hotel for {horizon_days - 1} nights"
        elif horizon_days > 1 and hotel_anchor:
            summary += f", using {hotel_anchor} as the shared hotel for {horizon_days - 1} nights"
        elif hotel_anchor and anchor_policy == "round_trip":
            summary += f", starting and ending at {hotel_anchor}"
        elif hotel_anchor and anchor_policy == "start_only":
            summary += f", starting at {hotel_anchor}"
        elif hotel_anchor and anchor_policy == "distinct_end":
            summary += f", starting at {hotel_anchor}"
            if end_anchor:
                summary += f" and ending at {end_anchor}"
        if horizon_days > 1:
            travelers = preferences.get("travelers")
            rooms = preferences.get("rooms")
            if travelers not in (None, "") and rooms not in (None, ""):
                traveler_label = "traveler" if str(travelers) == "1" else "travelers"
                room_label = "room" if str(rooms) == "1" else "rooms"
                summary += f", for {travelers} {traveler_label} in {rooms} {room_label}"
        fields_by_key = {
            str(field.get("key")): field
            for field in form.get("fields") or []
            if isinstance(field, dict)
        }
        missing_labels = [
            str((fields_by_key.get(str(key)) or {}).get("label") or key).lower()
            for key in form.get("missing_required") or []
        ]
        if missing_labels:
            names = _list_phrase(missing_labels)
            message = (
                f"{summary}. {names[0].upper()}{names[1:]} "
                f"{'isn' if len(missing_labels) == 1 else 'aren'}'t set yet; "
                f"fill {'it' if len(missing_labels) == 1 else 'them'} in below, then confirm to start planning."
            )
        else:
            message = f"{summary}. Review these constraints, then confirm to start planning."
    confirmation = {
        "message": message,
        "preferences": preferences,
        "needs_confirmation": True,
        "preference_form": form,
    }
    options = preferences.get("location_options")
    selected = str(preferences.get("location_resolution") or "") == "selected"
    if isinstance(options, list) and len(options) > 1 and not selected:
        actions = []
        for index, option in enumerate(options[:4]):
            if isinstance(option, dict):
                label = str(option.get("label") or option.get("value") or "").strip()
                value = str(option.get("value") or option.get("label") or "").strip()
            else:
                label = value = str(option).strip()
            if not value:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or f"location_{index}"
            actions.append({
                "id": f"location_{slug}"[:64],
                "label": label[:40],
                "value": value,
                "preference_patch": {"location": value, "location_resolution": "selected"},
                "message": label[:80],
            })
        if len(actions) > 1:
            confirmation["message"] = "Which destination did you mean?"
            confirmation["quick_actions"] = actions
    anchor_clarification = preferences.get("_anchor_clarification")
    if isinstance(anchor_clarification, dict):
        anchor_key = str(anchor_clarification.get("key") or "start")
        anchor_options = anchor_clarification.get("options")
        actions = []
        for index, option in enumerate(anchor_options if isinstance(anchor_options, list) else []):
            if not isinstance(option, dict):
                continue
            label = str(option.get("resolved_name") or option.get("address") or "").strip()
            if not label:
                continue
            existing = preferences.get("resolved_anchors")
            resolved_anchors = dict(existing) if isinstance(existing, dict) else {}
            resolved_anchors[anchor_key] = option
            slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or f"anchor_{index}"
            actions.append({
                "id": f"anchor_{anchor_key}_{slug}"[:64],
                "label": label[:40],
                "value": str(option.get("provider_id") or label),
                "preference_patch": {
                    "resolved_anchors": resolved_anchors,
                    "_anchor_clarification": None,
                },
                "message": label[:80],
            })
        preference_key = "hotel_anchor" if anchor_key == "start" else "end_anchor"
        continue_label = "Continue without a start anchor" if anchor_key == "start" else "Continue without an end anchor"
        actions.append({
            "id": f"anchor_{anchor_key}_none",
            "label": continue_label,
            "value": "none",
            "preference_patch": (
                {"lodging_mode": "none"}
                if anchor_key == "start"
                else {"anchor_policy": "start_only"}
            ),
            "clear_preference_keys": [preference_key],
            "message": continue_label,
        })
        if len(actions) > 1:
            qualifier = "starting" if anchor_key == "start" else "ending"
            confirmation["message"] = f"Which {qualifier} place did you mean?"
            confirmation["quick_actions"] = actions
    elif preferences.get("_anchor_resolution_error"):
        anchor_key = str(preferences.get("_anchor_resolution_error") or "start")
        preference_key = "hotel_anchor" if anchor_key == "start" else "end_anchor"
        continue_label = "Continue without a start anchor" if anchor_key == "start" else "Continue without an end anchor"
        confirmation["message"] = (
            "I couldn't resolve that itinerary anchor to a unique place. "
            "Provide its exact name and address, or continue without that anchor."
        )
        confirmation["quick_actions"] = [{
            "id": f"anchor_{anchor_key}_none",
            "label": continue_label,
            "value": "none",
            "preference_patch": (
                {"lodging_mode": "none"}
                if anchor_key == "start"
                else {"anchor_policy": "start_only"}
            ),
            "clear_preference_keys": [preference_key],
            "message": continue_label,
        }]
    return confirmation


def _itinerary_form_incomplete(confirmation: Dict[str, Any], preferences: Dict[str, Any]) -> bool:
    form = confirmation.get("preference_form")
    missing = form.get("missing_required") if isinstance(form, dict) else []
    unresolved_location = bool(
        isinstance(preferences.get("location_options"), list)
        and len(preferences.get("location_options") or []) > 1
        and str(preferences.get("location_resolution") or "") != "selected"
    )
    try:
        horizon_days = int(preferences.get("horizon_days") or 1)
        unsupported_horizon = not 1 <= horizon_days <= 3
    except (TypeError, ValueError):
        unsupported_horizon = True
    anchor_policy = str(preferences.get("anchor_policy") or "round_trip")
    resolved_anchors = preferences.get("resolved_anchors")
    resolved_anchors = resolved_anchors if isinstance(resolved_anchors, dict) else {}
    unresolved_anchor = bool(
        _itinerary_lodging_mode(None, preferences) == "supplied"
        and str(preferences.get("hotel_anchor") or "").strip()
        and not isinstance(resolved_anchors.get("start"), dict)
    )
    if anchor_policy == "distinct_end" and str(preferences.get("end_anchor") or "").strip():
        unresolved_anchor = unresolved_anchor or not isinstance(resolved_anchors.get("end"), dict)
    return bool(missing or unresolved_location or unsupported_horizon or unresolved_anchor)


_ITINERARY_VALIDATION_FIELDS = {
    "missing_location": ("location",),
    "missing_timezone": ("timezone",),
    "invalid_date": ("date",),
    "invalid_time_window": ("daily_start_time", "daily_end_time"),
    "unsupported_horizon": ("horizon_days",),
    "invalid_budget_mode": ("budget_mode",),
    "missing_budget_amount": ("budget_amount",),
    "missing_budget_currency": ("budget_currency",),
    "missing_travelers": ("travelers",),
    "missing_rooms": ("rooms",),
    "missing_lodging_requirement": ("hotel_anchor",),
    "invalid_lodging_mode": ("hotel_anchor",),
}

_ITINERARY_VALIDATION_MESSAGES = {
    "missing_location": "Destination is required.",
    "missing_timezone": "Timezone is required.",
    "invalid_date": "First travel date must be a valid date.",
    "invalid_time_window": "Daily end time must be later than daily start time.",
    "unsupported_horizon": "Trip length must be between one and three days.",
    "invalid_budget_mode": "Budget must be limited or unlimited.",
    "missing_budget_amount": "A limited budget must be greater than zero.",
    "missing_budget_currency": "A limited budget needs a currency.",
    "missing_travelers": "Traveler count must be greater than zero.",
    "missing_rooms": "Room count must be greater than zero.",
    "missing_lodging_requirement": "A multi-day itinerary needs one shared hotel.",
    "invalid_lodging_mode": "A multi-day itinerary needs one shared hotel.",
}


def _itinerary_validation_confirmation(
    confirmation: Dict[str, Any],
    errors: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Turn semantic IR violations into an editable HITL clarification."""
    codes: List[str] = []
    for error in errors:
        code = str(error.get("code") or "") if isinstance(error, dict) else ""
        if code and code not in codes:
            codes.append(code)
    messages = [
        _ITINERARY_VALIDATION_MESSAGES.get(code, "One or more itinerary constraints are invalid.")
        for code in codes
    ]
    result = dict(confirmation or {})
    result["message"] = " ".join([*messages, "Update the fields below, then confirm again."])

    form = result.get("preference_form")
    if isinstance(form, dict):
        form = dict(form)
        missing = list(form.get("missing_required") or [])
        for code in codes:
            for key in _ITINERARY_VALIDATION_FIELDS.get(code, ()):
                if key not in missing:
                    missing.append(key)
        form["missing_required"] = missing
        form["complete"] = False
        result["preference_form"] = form
    return result


def _enrich_itinerary_preferences(
    preferences: Dict[str, Any],
    user_profile: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    enriched = dict(preferences or {})
    sources = dict(enriched.get("_itinerary_field_sources") or {})
    for key, value in list(enriched.items()):
        if value not in (None, "", [], {}) and not key.startswith("_"):
            sources.setdefault(key, "user")
    if not str(enriched.get("location") or "").strip() and isinstance(user_profile, dict):
        demographics = user_profile.get("demographics")
        profile_location = demographics.get("location") if isinstance(demographics, dict) else None
        if str(profile_location or "").strip():
            enriched["location"] = str(profile_location).strip()
            sources["location"] = "profile"
    location_lower = str(enriched.get("location") or "").lower()
    if not enriched.get("timezone") and any(token in location_lower for token in ("singapore", "sentosa", "ntu", "chinatown")):
        enriched["timezone"] = "Asia/Singapore"
        sources["timezone"] = "system"
    if not enriched.get("pace"):
        enriched["pace"] = "balanced"
        sources["pace"] = "system"
    if not enriched.get("style"):
        enriched["style"] = "sightseeing"
        sources["style"] = "system"
    from langgraph_metarec.itinerary_policy import infer_itinerary_attraction_types

    inferred_types = infer_itinerary_attraction_types(str(enriched.get("query") or ""))
    existing_types = enriched.get("attraction_types")
    if isinstance(existing_types, str):
        existing_types = [part.strip() for part in existing_types.split(",") if part.strip()]
    elif not isinstance(existing_types, (list, tuple, set)):
        existing_types = []
    merged_types = list(dict.fromkeys([
        *(str(value).strip() for value in existing_types if str(value).strip()),
        *inferred_types,
    ]))
    if merged_types:
        enriched["attraction_types"] = merged_types
        if inferred_types:
            sources["attraction_types"] = "user"
    interest_terms = enriched.get("interest_terms")
    if isinstance(interest_terms, str):
        interest_terms = [part.strip() for part in interest_terms.split(",") if part.strip()]
    elif not isinstance(interest_terms, (list, tuple, set)):
        interest_terms = []
    merged_terms = list(dict.fromkeys([
        *(str(value).strip() for value in interest_terms if str(value).strip()),
        *(value.replace("-", " ") for value in inferred_types),
    ]))
    if merged_terms:
        enriched["interest_terms"] = merged_terms[:8]
        sources["interest_terms"] = "user"
    if not enriched.get("horizon_days"):
        enriched["horizon_days"] = 1
        sources["horizon_days"] = "system"
    if not enriched.get("daily_start_time") and enriched.get("start_time"):
        enriched["daily_start_time"] = enriched["start_time"]
        sources["daily_start_time"] = sources.get("start_time", "user")
    if not enriched.get("daily_end_time") and enriched.get("end_time"):
        enriched["daily_end_time"] = enriched["end_time"]
        sources["daily_end_time"] = sources.get("end_time", "user")
    horizon_match = re.search(
        # Digits pair with either unit: "3 days" via the first branch, "3天"
        # via the second — the Latin branch alone left "3天" unmatched.
        r"(?:(one|two|three|1|2|3)[ -]?days?|([一二两三123])[日天])",
        str(enriched.get("query") or ""),
        re.IGNORECASE,
    )
    if horizon_match:
        token = str(horizon_match.group(1) or horizon_match.group(2) or "").lower()
        enriched["horizon_days"] = {
            "one": 1, "1": 1, "一": 1,
            "two": 2, "2": 2, "二": 2, "两": 2,
            "three": 3, "3": 3, "三": 3,
        }.get(token, enriched.get("horizon_days", 1))
        sources["horizon_days"] = "user"
    if str(enriched.get("hotel_anchor") or "").strip() and not enriched.get("anchor_policy"):
        try:
            is_multi_day = int(enriched.get("horizon_days") or 1) > 1
        except (TypeError, ValueError):
            is_multi_day = False
        enriched["anchor_policy"] = "start_only" if is_multi_day else "round_trip"
        sources["anchor_policy"] = "system"
    enriched["_itinerary_field_sources"] = sources
    return enriched


async def _resolve_itinerary_anchor_preferences(
    adapters: RequestOrchestratorAdapters,
    preferences: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve configured route anchors before a task can be created."""
    resolver = adapters.resolve_itinerary_anchor
    if resolver is None:
        return preferences
    enriched = dict(preferences)
    enriched.pop("_anchor_clarification", None)
    enriched.pop("_anchor_resolution_error", None)
    resolved = enriched.get("resolved_anchors")
    resolved = dict(resolved) if isinstance(resolved, dict) else {}
    attempts = enriched.get("_anchor_resolution_attempts")
    attempts = dict(attempts) if isinstance(attempts, dict) else {}
    destination = str(enriched.get("resolved_location") or enriched.get("location") or "").strip()
    anchor_policy = str(enriched.get("anchor_policy") or "round_trip")
    requested = []
    if _itinerary_lodging_mode(None, enriched) == "supplied":
        requested.append(("start", "hotel_anchor"))
    if anchor_policy == "distinct_end":
        requested.append(("end", "end_anchor"))
    from langgraph_metarec.itinerary_anchors import resolve_anchor_candidates

    for anchor_key, preference_key in requested:
        anchor_query = str(enriched.get(preference_key) or "").strip()
        if not anchor_query:
            resolved.pop(anchor_key, None)
            attempts.pop(anchor_key, None)
            continue
        previous = resolved.get(anchor_key)
        if isinstance(previous, dict) and str(previous.get("query") or "").strip() == anchor_query:
            continue
        resolved.pop(anchor_key, None)
        fingerprint = "|".join((
            "anchor-resolution/v1",
            anchor_key,
            " ".join(anchor_query.lower().split()),
            " ".join(destination.lower().split()),
        ))
        previous_attempt = attempts.get(anchor_key)
        if isinstance(previous_attempt, dict) and previous_attempt.get("fingerprint") == fingerprint:
            status = str(previous_attempt.get("status") or "unresolved")
            if status == "ambiguous":
                enriched["_anchor_clarification"] = {
                    "key": anchor_key,
                    "options": list(previous_attempt.get("options") or []),
                }
            elif status == "unresolved":
                enriched["_anchor_resolution_error"] = anchor_key
            break
        candidates = await resolver(anchor_query, destination, None)
        resolution = resolve_anchor_candidates(anchor_query, destination, candidates)
        if resolution.status == "resolved" and resolution.match is not None:
            resolved[anchor_key] = resolution.match
            attempts[anchor_key] = {"fingerprint": fingerprint, "status": "resolved"}
            continue
        if resolution.status == "ambiguous":
            options = list(resolution.options)
            enriched["_anchor_clarification"] = {
                "key": anchor_key,
                "options": options,
            }
            attempts[anchor_key] = {
                "fingerprint": fingerprint,
                "status": "ambiguous",
                "options": options,
            }
        else:
            enriched["_anchor_resolution_error"] = anchor_key
            attempts[anchor_key] = {"fingerprint": fingerprint, "status": "unresolved"}
        break
    enriched["resolved_anchors"] = resolved
    enriched["_anchor_resolution_attempts"] = attempts
    return enriched


def _itinerary_gather_tasks(route: Dict[str, Any], planning_request: Dict[str, Any]) -> List[Dict[str, Any]]:
    domains = ["attraction"]
    hard = planning_request.get("hard_constraints") or {}
    soft = planning_request.get("soft_preferences") or {}
    if hard.get("meal_obligations") or soft.get("suggested_meals"):
        domains.append("restaurant")
    lodging = planning_request.get("lodging")
    if isinstance(lodging, dict) and lodging.get("mode") == "recommend":
        domains.append("hotel")
    return [
        {
            "domain": domain,
            "source_domain": domain,
            "status": "ready",
            "tool_tags": tool_tags_for_domain(domain),
        }
        for domain in domains
    ]


def _meaningful_preference_overlay(incoming: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Drop empty values from a client-submitted preference overlay: a pristine
    form field arrives as "" / [] and must never clobber an extracted value
    (e.g. an empty location wiping the destination the LLM pulled from the
    query). Clearing a field intentionally is not a supported gesture — refine
    flows replace values instead."""
    if not isinstance(incoming, dict):
        return {}
    return {
        key: value
        for key, value in incoming.items()
        if key != "clear_preference_keys" and value not in (None, "", [], {})
    }


def _itinerary_anchor_missing(route: Optional[Dict[str, Any]], preferences: Optional[Dict[str, Any]]) -> bool:
    """True when the route was asked to start from the user's hotel but no
    concrete hotel anchor has been provided yet."""
    if not isinstance(route, dict) or route.get("mode") != "itinerary":
        return False
    if _itinerary_lodging_mode(route, preferences or {}) != "supplied":
        return False
    return not str((preferences or {}).get("hotel_anchor") or "").strip()


def _multi_domain_confirmation(query: str, route: Dict[str, Any], preferences: Dict[str, Any]) -> Dict[str, Any]:
    """A coordination confirmation listing the ready domains. Multi-domain stays a
    simple yes/no (no single preference form)."""
    ready_domains = [
        task.get("domain")
        for task in route.get("domain_tasks", [])
        if isinstance(task, dict) and task.get("status") == "ready"
    ]
    label = ", ".join([str(item) for item in ready_domains if item]) or "multiple domains"
    return {
        "message": f"I detected this as a multi-domain recommendation request ({label}). I'll search for: {query}. Is that correct?",
        "preferences": preferences,
        "needs_confirmation": True,
    }


def _attach_preference_form(confirmation: Dict[str, Any], domain: str, preferences: Dict[str, Any]) -> None:
    """Attach the request-time preference form for ``domain`` so the client can
    refine structured preferences before the search runs. Single-domain only."""
    try:
        from preference_specs import build_domain_form

        form = build_domain_form(str(domain), preferences)
        if form.get("fields"):
            confirmation["preference_form"] = form
    except Exception:
        pass


HITL_EXPIRY_SECONDS = int(os.getenv("HITL_EXPIRY_SECONDS", "3600"))


def _is_collecting(runtime: GraphRuntimeState) -> bool:
    collect = runtime.collect_confirm_state or {}
    if collect.get("status") not in {"awaiting_confirmation", "awaiting_clarification"}:
        return False
    created_at_str = collect.get("created_at")
    if created_at_str:
        try:
            created_at = _dt.datetime.fromisoformat(created_at_str)
            if created_at.tzinfo is None:
                # Legacy-compat only: states persisted before created_at became
                # timezone-aware were stamped with naive server-local time. New
                # states carry an explicit UTC offset and skip this branch.
                created_at = created_at.replace(tzinfo=_dt.timezone.utc)
            elapsed = (_dt.datetime.now(_dt.timezone.utc) - created_at).total_seconds()
            if elapsed > HITL_EXPIRY_SECONDS:
                return False
        except (ValueError, TypeError):
            pass  # malformed timestamp → treat as non-expired (safe default)
    return True


def build_request_orchestrator_graph(
    adapters: RequestOrchestratorAdapters,
    *,
    checkpointer: Optional[Any] = None,
    trace_ctx: Optional[TraceContext] = None,
):
    async def intention_node(state: RequestOrchestratorState) -> RequestOrchestratorState:
        runtime = GraphRuntimeState.from_checkpoint(state.get("runtime"))
        collect_state = runtime.collect_confirm_state
        if (
            isinstance(collect_state, dict)
            and collect_state.get("action") == "confirm"
            and _is_collecting(runtime)
        ):
            runtime.intent_result = IntentResult(
                intent="confirmation_yes",
                confidence=1.0,
                preferences=collect_state.get("preferences"),
            )
            return {**state, "runtime": runtime.to_checkpoint()}

        history = list(state.get("conversation_history") or [])
        confirmation_message = _confirmation_message_from_hitl(collect_state)
        if _is_collecting(runtime) and confirmation_message:
            if not history or history[-1].get("content", "").strip() != confirmation_message.strip():
                history.append({"role": "assistant", "content": confirmation_message})

        pending_preferences = collect_state.get("preferences") if isinstance(collect_state, dict) else None
        try:
            llm_response = await adapters.analyze_message(
                runtime.query,
                history,
                state.get("user_profile"),
                _is_collecting(runtime),
                pending_preferences,
            )
            runtime.intent_result = IntentResult(
                intent=llm_response.intent,
                confidence=llm_response.confidence,
                reply=llm_response.reply,
                preferences=llm_response.preferences,
                profile_updates=getattr(llm_response, "profile_updates", None),
            )
        except Exception as exc:
            runtime.intent_result = IntentResult(intent="chat", confidence=0.0, reply=str(exc))
            runtime.errors.append(RuntimeErrorRecord(message=str(exc), node="intention"))
        return {**state, "runtime": runtime.to_checkpoint()}

    async def collect_confirm_node(state: RequestOrchestratorState) -> RequestOrchestratorState:
        runtime = GraphRuntimeState.from_checkpoint(state.get("runtime"))
        intent = runtime.intent_result.intent if runtime.intent_result else "chat"
        collect_state = runtime.collect_confirm_state or {}
        collecting = _is_collecting(runtime)
        preferences = runtime.intent_result.preferences if runtime.intent_result else None
        hitl_action = collect_state.get("action") if isinstance(collect_state, dict) else None

        if collecting and hitl_action == "reject":
            previous = collect_state.get("preferences") if isinstance(collect_state, dict) else None
            routing = collect_state.get("routing") if isinstance(collect_state, dict) else None
            refine_query = collect_state.get("query") or runtime.query
            domain_for_confirm = _route_domain(routing)
            # Domain-aware overlay of any newly stated preferences onto the set under
            # review — the same path as a text rejection — so a movie/music/book/
            # product rejection keeps its structured prefs and still gets the
            # request-time form (not a restaurant-shaped, form-less message).
            resolved_preferences = _refine_preferences(
                routing=routing,
                previous=previous,
                new=preferences,
                restaurant_baseline=state.get("restaurant_baseline"),
            )
            resolved_preferences = _normalize_domain_preferences(
                domain_for_confirm, resolved_preferences, refine_query
            )
            confirmation = _modification_confirmation(resolved_preferences)
            if (routing or {}).get("mode") != "multi_domain":
                _attach_preference_form(confirmation, domain_for_confirm, resolved_preferences)
            runtime.intent_result = IntentResult(
                intent="confirmation_no",
                confidence=runtime.intent_result.confidence if runtime.intent_result else None,
                reply=runtime.intent_result.reply if runtime.intent_result else None,
                preferences=resolved_preferences,
                profile_updates=runtime.intent_result.profile_updates if runtime.intent_result else None,
            )
            runtime.collect_confirm_state = build_collect_confirm_state_payload(
                query=refine_query,
                intent="confirmation_no",
                preferences=resolved_preferences,
                pending_preferences=resolved_preferences,
                needs_confirmation=True,
                confirmation_request=confirmation,
                routing=routing,
                status="awaiting_clarification",
            )
            runtime.collect_confirm_state["action"] = "reject"
            runtime.response_payload = {
                "type": "confirmation",
                "confirmation_request": confirmation,
                "intent": "confirmation_no",
                "preferences": resolved_preferences,
                "hitl_state": runtime.collect_confirm_state,
            }
            return {**state, "runtime": runtime.to_checkpoint()}

        if intent == "chat":
            runtime.collect_confirm_state = None
            runtime.response_payload = {
                "type": "llm_reply",
                "llm_reply": runtime.intent_result.reply if runtime.intent_result else "",
                "intent": "chat",
                "confidence": runtime.intent_result.confidence if runtime.intent_result else 0.0,
                "preferences": preferences,
            }
            return {**state, "runtime": runtime.to_checkpoint()}

        if intent == "confirmation_yes" and collecting:
            runtime.collect_confirm_state = {
                **collect_state,
                "status": "confirmed",
                "intent": intent,
            }
            runtime.response_payload = {"type": "confirmed"}
            return {**state, "runtime": runtime.to_checkpoint()}

        if collecting and intent in {"confirmation_no", "query"}:
            previous = collect_state.get("preferences") if isinstance(collect_state, dict) else None
            previous_route = collect_state.get("routing") if isinstance(collect_state, dict) else None
            starts_new_flow = intent == "query" and _starts_new_query_flow(
                collect_state,
                runtime.query,
                preferences,
            )
            # Refine the set under review unless the user clearly started a new
            # recommendation request/domain while a prior confirmation was open.
            if starts_new_flow:
                preferences = preferences or {}
                original_query = runtime.query
                routing = None
            else:
                preferences = _refine_preferences(
                    routing=previous_route,
                    previous=previous,
                    new=preferences,
                    restaurant_baseline=state.get("restaurant_baseline"),
                )
                original_query = collect_state.get("query") or runtime.query
                routing = previous_route

            if intent == "query":
                # An in-flow query still flows on to routing -> domain_dispatch,
                # which owns the single make_confirmation call. Only stage the
                # refined preferences here (mirroring the fresh-query path) so the
                # confirmation LLM is not generated twice and then discarded.
                runtime.collect_confirm_state = build_collect_confirm_state_payload(
                    query=original_query,
                    intent=intent,
                    preferences=preferences or {},
                    pending_preferences=preferences or {},
                    needs_confirmation=True,
                    routing=routing,
                    status="awaiting_confirmation",
                )
                runtime.response_payload = {
                    "type": "pending_confirmation",
                    "preferences": preferences or {},
                    "hitl_state": runtime.collect_confirm_state,
                }
                return {**state, "runtime": runtime.to_checkpoint()}

            # confirmation_no terminates at the result node (no dispatch), so it
            # builds its own confirmation here — same natural message + form as the
            # ready-domain path.
            domain_for_confirm = _route_domain(routing)
            preferences = _normalize_domain_preferences(domain_for_confirm, preferences or {}, original_query)
            confirmation = await adapters.make_confirmation(original_query, preferences or {}, domain_for_confirm)
            if (routing or {}).get("mode") != "multi_domain":
                _attach_preference_form(confirmation, domain_for_confirm, preferences or {})
            runtime.collect_confirm_state = build_collect_confirm_state_payload(
                query=original_query,
                intent=intent,
                preferences=preferences or {},
                pending_preferences=preferences or {},
                needs_confirmation=True,
                confirmation_request=confirmation,
                routing=routing,
                status="awaiting_confirmation",
            )
            runtime.response_payload = {
                "type": "confirmation",
                "confirmation_request": confirmation,
                "intent": intent,
                "preferences": preferences or {},
                "hitl_state": runtime.collect_confirm_state,
            }
            return {**state, "runtime": runtime.to_checkpoint()}

        if intent == "query":
            # Do not seed fresh queries with restaurant defaults before routing:
            # a movie/book/music/product request should not carry a restaurant
            # preference baseline into HITL. Restaurant routes fuse/extract their
            # baseline in domain_dispatch once the route is known.
            preferences = preferences or {}
            runtime.collect_confirm_state = build_collect_confirm_state_payload(
                query=runtime.query,
                intent=intent,
                preferences=preferences or {},
                pending_preferences=preferences or {},
                needs_confirmation=True,
                status="awaiting_confirmation",
            )
            runtime.response_payload = {
                "type": "pending_confirmation",
                "preferences": preferences or {},
                "hitl_state": runtime.collect_confirm_state,
            }
        return {**state, "runtime": runtime.to_checkpoint()}

    async def routing_node(state: RequestOrchestratorState) -> RequestOrchestratorState:
        runtime = GraphRuntimeState.from_checkpoint(state.get("runtime"))
        intent = runtime.intent_result.intent if runtime.intent_result else None
        if intent not in {"query", "confirmation_yes"}:
            return state

        collect_state = runtime.collect_confirm_state or {}
        existing_route = collect_state.get("routing") if isinstance(collect_state.get("routing"), dict) else None
        if intent == "confirmation_yes" and existing_route:
            runtime.routing_route = existing_route
            return {**state, "runtime": runtime.to_checkpoint()}

        preferences = collect_state.get("preferences") if isinstance(collect_state, dict) else None
        if not preferences and runtime.intent_result:
            preferences = runtime.intent_result.preferences
        route = await run_routing_graph(
            query=collect_state.get("query") or runtime.query,
            intent=intent,
            preferences=preferences,
            domain_lock=state.get("domain_lock"),
            force_itinerary=bool(state.get("itinerary_mode")),
            **trace_kwargs(trace_ctx),
        )
        runtime.routing_route = _route_to_dict(route, state.get("domain_lock"))
        if runtime.collect_confirm_state is not None:
            runtime.collect_confirm_state["routing"] = runtime.routing_route
            if runtime.response_payload.get("hitl_state"):
                runtime.response_payload["hitl_state"] = runtime.collect_confirm_state
        return {**state, "runtime": runtime.to_checkpoint()}

    async def domain_dispatch_node(state: RequestOrchestratorState) -> RequestOrchestratorState:
        runtime = GraphRuntimeState.from_checkpoint(state.get("runtime"))
        route = runtime.routing_route or {}
        intent = runtime.intent_result.intent if runtime.intent_result else None
        collect_state = runtime.collect_confirm_state or {}

        if intent == "query" and route and route.get("status") != "ready":
            # We can't serve this query (ambiguous/unknown, or a recognized domain
            # that isn't connected). Respond as-is with a graceful, extendable reply
            # that points to the supported domains — no clarification HITL loop.
            status = route.get("status")
            runtime.collect_confirm_state = None
            runtime.response_payload = {
                "type": "llm_reply",
                "llm_reply": _unsupported_domain_reply(route.get("domain")),
                "intent": status,
                "confidence": route.get("domain_confidence"),
                "preferences": runtime.intent_result.preferences if runtime.intent_result else None,
                "domain": route.get("domain"),
                "routing": route,
            }
            return {**state, "runtime": runtime.to_checkpoint()}

        if intent == "query" and route.get("status") == "ready":
            # One confirmation path for every ready domain: a natural, domain-aware
            # message (restaurant/movie/music/book/product all flow through the same
            # make_confirmation) plus the request-time preference form. Multi-domain
            # stays a coordination yes/no.
            original_query = collect_state.get("query") or runtime.query
            exec_domain = _route_domain(route)
            is_multi = route.get("mode") == "multi_domain"

            if exec_domain == "restaurant" and not is_multi:
                # Restaurant has a rich preference baseline to fuse/extract.
                raw_preferences = collect_state.get("preferences") or {}
                base_preferences = state.get("restaurant_baseline") or {}
                if raw_preferences:
                    preferences = merge_preferences(base_preferences, raw_preferences)
                else:
                    preferences = adapters.extract_preferences(original_query)
            else:
                preferences = {
                    **_generic_preference_subset(collect_state.get("preferences")),
                    "domain": route.get("domain"),
                    "query": original_query,
                }
                preferences = _normalize_domain_preferences(exec_domain, preferences, original_query)

            if exec_domain == "hotel" and not is_multi:
                preferences = _enrich_hotel_preferences_from_profile(preferences, state.get("user_profile"))
                if _hotel_location_needs_clarification(preferences, state.get("user_profile")):
                    confirmation = _hotel_location_clarification(preferences)
                    _attach_preference_form(confirmation, exec_domain, preferences)
                    runtime.intent_result = IntentResult(
                        intent="confirmation_no",
                        confidence=runtime.intent_result.confidence if runtime.intent_result else None,
                        reply=runtime.intent_result.reply if runtime.intent_result else None,
                        preferences=preferences,
                        profile_updates=runtime.intent_result.profile_updates if runtime.intent_result else None,
                    )
                    runtime.collect_confirm_state = build_collect_confirm_state_payload(
                        query=original_query,
                        intent="confirmation_no",
                        preferences=preferences,
                        pending_preferences=preferences,
                        needs_confirmation=True,
                        confirmation_request=confirmation,
                        routing=route,
                        status="awaiting_clarification",
                    )
                    runtime.response_payload = {
                        "type": "confirmation",
                        "confirmation_request": confirmation,
                        "intent": "confirmation_no",
                        "preferences": preferences,
                        "domain": route.get("domain"),
                        "hitl_state": runtime.collect_confirm_state,
                    }
                    return {**state, "runtime": runtime.to_checkpoint()}

            if route.get("mode") == "itinerary":
                if adapters.extract_itinerary_constraints is not None:
                    try:
                        extracted = await adapters.extract_itinerary_constraints(original_query, preferences)
                    except Exception:
                        extracted = None
                    if isinstance(extracted, dict):
                        preferences = {**preferences, **_meaningful_preference_overlay(extracted)}
                preferences["query"] = original_query
                preferences = _enrich_itinerary_preferences(preferences, state.get("user_profile"))
                preferences = _with_itinerary_lodging_mode(route, preferences)
                preferences = await _resolve_itinerary_anchor_preferences(adapters, preferences)
                confirmation = _itinerary_confirmation(original_query, route, preferences)
                if not _itinerary_form_incomplete(confirmation, preferences):
                    from langgraph_metarec.itinerary_contracts import planning_request_from_preferences

                    planning_request, planning_errors = planning_request_from_preferences(preferences)
                    if planning_request is not None and not planning_errors:
                        runtime.metadata.pop("itinerary_validation_errors", None)
                        route = {
                            **route,
                            "domain_tasks": _itinerary_gather_tasks(route, planning_request.to_dict()),
                            "metadata": {
                                **(route.get("metadata") or {}),
                                "planning_request": planning_request.to_dict(),
                            },
                        }
                        runtime.routing_route = route
                    else:
                        planning_errors = planning_errors or [{"code": "invalid_planning_request"}]
                        runtime.metadata["itinerary_validation_errors"] = planning_errors
                        confirmation = _itinerary_validation_confirmation(confirmation, planning_errors)
            elif is_multi:
                confirmation = _multi_domain_confirmation(original_query, route, preferences)
            else:
                # Round 1 stays light: a natural message plus any quick actions from
                # make_confirmation. The full request-time form is reserved for the
                # refine round (reject / confirmation_no), so we do NOT attach it here.
                confirmation = await adapters.make_confirmation(original_query, preferences, exec_domain)

            runtime.collect_confirm_state = build_collect_confirm_state_payload(
                query=original_query,
                intent=intent,
                preferences=preferences,
                pending_preferences=preferences,
                needs_confirmation=True,
                confirmation_request=confirmation,
                routing=route,
                status=(
                    "awaiting_clarification"
                    if route.get("mode") == "itinerary" and _itinerary_form_incomplete(confirmation, preferences)
                    else "awaiting_confirmation"
                ),
            )
            runtime.response_payload = {
                "type": "confirmation",
                "confirmation_request": confirmation,
                "preferences": preferences,
                "domain": route.get("domain"),
                "hitl_state": runtime.collect_confirm_state,
            }
            return {**state, "runtime": runtime.to_checkpoint()}

        if intent == "confirmation_yes":
            preferences = collect_state.get("preferences") or {}
            original_query = collect_state.get("query") or runtime.query
            planning_request = None
            planning_errors: List[Dict[str, Any]] = []
            if isinstance(route, dict) and route.get("mode") == "itinerary":
                preferences = _enrich_itinerary_preferences(preferences, state.get("user_profile"))
                preferences = _with_itinerary_lodging_mode(route, preferences)
                preferences = await _resolve_itinerary_anchor_preferences(adapters, preferences)
                confirmation = _itinerary_confirmation(original_query, route, preferences)
                if not _itinerary_form_incomplete(confirmation, preferences):
                    from langgraph_metarec.itinerary_contracts import planning_request_from_preferences

                    planning_request, planning_errors = planning_request_from_preferences(preferences)
                    if planning_request is None or planning_errors:
                        planning_errors = planning_errors or [{"code": "invalid_planning_request"}]
                        runtime.metadata["itinerary_validation_errors"] = planning_errors
                        confirmation = _itinerary_validation_confirmation(confirmation, planning_errors)
                    else:
                        runtime.metadata.pop("itinerary_validation_errors", None)
            else:
                confirmation = {}
            if _itinerary_anchor_missing(route, preferences) or (
                isinstance(route, dict)
                and route.get("mode") == "itinerary"
                and _itinerary_form_incomplete(confirmation, preferences)
            ):
                # Server-side enforcement of the required-field gate: confirming
                # without the requested hotel anchor re-opens the clarification
                # instead of creating a task with an unanchored route.
                runtime.intent_result = IntentResult(
                    intent="confirmation_no",
                    confidence=runtime.intent_result.confidence if runtime.intent_result else None,
                    reply=runtime.intent_result.reply if runtime.intent_result else None,
                    preferences=preferences,
                    profile_updates=runtime.intent_result.profile_updates if runtime.intent_result else None,
                )
                runtime.collect_confirm_state = build_collect_confirm_state_payload(
                    query=original_query,
                    intent="confirmation_no",
                    preferences=preferences,
                    pending_preferences=preferences,
                    needs_confirmation=True,
                    confirmation_request=confirmation,
                    routing=route,
                    status="awaiting_clarification",
                )
                runtime.response_payload = {
                    "type": "confirmation",
                    "confirmation_request": confirmation,
                    "intent": "confirmation_no",
                    "preferences": preferences,
                    "domain": route.get("domain"),
                    "hitl_state": runtime.collect_confirm_state,
                }
                return {**state, "runtime": runtime.to_checkpoint()}
            if isinstance(route, dict) and route.get("mode") == "itinerary":
                # A complete, valid form is normalized above. Invalid input has
                # already returned through the clarification gate, never as a 500.
                if planning_request is None:
                    runtime.metadata["itinerary_validation_errors"] = [
                        {"code": "invalid_planning_request"}
                    ]
                    confirmation = _itinerary_validation_confirmation(
                        _itinerary_confirmation(original_query, route, preferences),
                        runtime.metadata["itinerary_validation_errors"],
                    )
                    runtime.collect_confirm_state = build_collect_confirm_state_payload(
                        query=original_query,
                        intent="confirmation_no",
                        preferences=preferences,
                        pending_preferences=preferences,
                        needs_confirmation=True,
                        confirmation_request=confirmation,
                        routing=route,
                        status="awaiting_clarification",
                    )
                    runtime.response_payload = {
                        "type": "confirmation",
                        "confirmation_request": confirmation,
                        "intent": "confirmation_no",
                        "preferences": preferences,
                        "domain": route.get("domain"),
                        "hitl_state": runtime.collect_confirm_state,
                    }
                    return {**state, "runtime": runtime.to_checkpoint()}
                route = {
                    **route,
                    "domain_tasks": _itinerary_gather_tasks(route, planning_request.to_dict()),
                    "metadata": {
                        **(route.get("metadata") or {}),
                        "planning_request": planning_request.to_dict(),
                    },
                }
                runtime.routing_route = route
            task_id = await adapters.create_task(original_query, preferences, route.get("tool_tags"), route)
            runtime.task_id = task_id
            runtime.task_status = TaskStatusProjection(
                task_id=task_id,
                status="pending",
                progress=0,
                message="Task created",
                metadata={"routing": route},
            )
            runtime.collect_confirm_state = None
            runtime.response_payload = {
                "type": "task_created",
                "task_id": task_id,
                "message": "Task started successfully",
                "preferences": preferences,
                "domain": route.get("domain"),
            }
        return {**state, "runtime": runtime.to_checkpoint()}

    def result_node(state: RequestOrchestratorState) -> RequestOrchestratorState:
        runtime = GraphRuntimeState.from_checkpoint(state.get("runtime"))
        payload = dict(runtime.response_payload)
        payload.setdefault("metadata", {})
        payload["metadata"] = {
            **payload["metadata"],
            **runtime.runtime_metadata(),
        }
        if runtime.routing_route and "routing" not in payload:
            payload["routing"] = runtime.routing_route
        if runtime.collect_confirm_state and "hitl_state" not in payload:
            payload["hitl_state"] = runtime.collect_confirm_state
        runtime.response_payload = payload
        return {**state, "runtime": runtime.to_checkpoint()}

    def _route_after_collect_confirm(state: RequestOrchestratorState) -> str:
        runtime = GraphRuntimeState.from_checkpoint(state.get("runtime"))
        intent = runtime.intent_result.intent if runtime.intent_result else None
        if intent in {"query", "confirmation_yes"}:
            return "routing"
        return "result"

    graph = StateGraph(RequestOrchestratorState)
    graph.add_node("intention", intention_node)
    graph.add_node("collect_confirm", collect_confirm_node)
    graph.add_node("routing", routing_node)
    graph.add_node("domain_dispatch", domain_dispatch_node)
    graph.add_node("result", result_node)
    graph.add_edge(START, "intention")
    graph.add_edge("intention", "collect_confirm")
    graph.add_conditional_edges(
        "collect_confirm",
        _route_after_collect_confirm,
        {"routing": "routing", "result": "result"},
    )
    graph.add_edge("routing", "domain_dispatch")
    graph.add_edge("domain_dispatch", "result")
    graph.add_edge("result", END)
    return graph.compile(checkpointer=checkpointer)


async def run_request_orchestrator(
    *,
    adapters: RequestOrchestratorAdapters,
    query: str,
    user_id: str,
    conversation_id: Optional[str],
    branch_id: Optional[str],
    message_id: Optional[str],
    conversation_history: Optional[List[Dict[str, Any]]],
    user_profile: Optional[Dict[str, Any]],
    restaurant_baseline: Optional[Dict[str, Any]],
    use_online_agent: bool,
    domain_lock: Optional[str],
    itinerary_mode: bool = False,
    hitl_state: Optional[Dict[str, Any]] = None,
    checkpointer: Optional[Any] = None,
    trace_ctx: Optional[TraceContext] = None,
) -> GraphRuntimeState:
    thread_id = conversation_thread_id(user_id, conversation_id, branch_id)
    owner = None
    if checkpointer is None:
        owner = RuntimeCheckpointer()
        active_checkpointer = await owner.aget()
    else:
        active_checkpointer = checkpointer
    graph = build_request_orchestrator_graph(adapters, checkpointer=active_checkpointer, trace_ctx=trace_ctx)
    config = {"configurable": {"thread_id": thread_id}}
    try:
        stored = await graph.aget_state(config)
        stored_runtime = stored.values.get("runtime") if stored and stored.values else None
        runtime = GraphRuntimeState.from_checkpoint(stored_runtime)
        runtime.user_id = user_id
        runtime.conversation_id = conversation_id
        runtime.branch_id = branch_id
        runtime.message_id = message_id
        runtime.thread_id = thread_id
        runtime.query = query
        if not runtime.collect_confirm_state and isinstance(hitl_state, dict):
            imported_state = dict(hitl_state)
            imported_preferences = imported_state.get("preferences")
            if isinstance(imported_preferences, dict):
                imported_state["preferences"] = _merge_hitl_preferences(
                    {},
                    imported_preferences,
                    imported_state.pop("clear_preference_keys", None),
                )
            runtime.collect_confirm_state = imported_state
            runtime.metadata["imported_hitl_state"] = True
        elif runtime.collect_confirm_state and isinstance(hitl_state, dict):
            # The client may submit refined preferences (e.g. request-time form
            # values picked during confirmation) alongside the confirm. The
            # checkpointed collect state owns the structure, but merge the
            # client's preferences so those choices actually reach the search
            # (explicit form values > checkpoint defaults).
            state_updates: Dict[str, Any] = {}
            base_preferences = runtime.collect_confirm_state.get("preferences") or {}
            merged_prefs = _merge_hitl_preferences(
                base_preferences,
                hitl_state.get("preferences"),
                hitl_state.get("clear_preference_keys"),
            )
            if merged_prefs != base_preferences:
                state_updates["preferences"] = merged_prefs
                runtime.metadata["merged_hitl_preferences"] = True
            incoming_action = hitl_state.get("action")
            if incoming_action in {"confirm", "reject"}:
                state_updates["action"] = incoming_action
                runtime.metadata["merged_hitl_action"] = incoming_action
            selected_quick_action = hitl_state.get("selected_quick_action")
            if isinstance(selected_quick_action, dict):
                state_updates["selected_quick_action"] = selected_quick_action
                runtime.metadata["selected_quick_action"] = selected_quick_action
            if state_updates:
                runtime.collect_confirm_state = {
                    **runtime.collect_confirm_state,
                    **state_updates,
                }

        final_state = await invoke_with_wp8_trace(
            graph,
            {
                "runtime": runtime.to_checkpoint(),
                "conversation_history": conversation_history or [],
                "user_profile": user_profile,
                "restaurant_baseline": restaurant_baseline,
                "use_online_agent": use_online_agent,
                "domain_lock": domain_lock,
                "itinerary_mode": itinerary_mode,
            },
            config,
            trace_ctx,
            graph_definition_id="request-orchestrator@v1",
        )
        return GraphRuntimeState.from_checkpoint(final_state["runtime"])
    finally:
        if owner is not None:
            await owner.aclose()
