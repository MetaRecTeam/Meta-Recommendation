from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from langgraph_metarec.nodes.domain import _keyword_score, classify_domain, detect_domains, domain_scores
from langgraph_metarec.tool_registry import normalize_tag
from langgraph_metarec.wp8_trace.invocation import TraceContext, invoke_with_wp8_trace


DOMAIN_TOOL_TAGS: Dict[str, List[str]] = {
    "restaurant": ["#place", "#restaurant"],
    "hotel": ["#place", "#hotel"],
    "attraction": ["#place", "#attraction"],
    "product": ["#thing", "#shopping", "#product"],
    "music": ["#thing", "#music"],
    "movie": ["#thing", "#movie"],
    "book": ["#thing", "#book"],
}

SUPPORTED_DOMAIN_LOCKS = set(DOMAIN_TOOL_TAGS)
EXECUTABLE_DOMAINS = {"restaurant", "hotel", "attraction", "product", "music", "movie", "book"}
ITINERARY_PLACE_DOMAINS = {"restaurant", "hotel", "attraction"}

# User-facing labels for the executable domains. This is the single, extendable
# source for the "what we support" message: connect a new domain by adding it to
# EXECUTABLE_DOMAINS (+ a label here) and the graceful fallback updates itself.
EXECUTABLE_DOMAIN_LABELS: Dict[str, str] = {
    "restaurant": "restaurants",
    "hotel": "hotels",
    "attraction": "tourist attractions",
    "movie": "movies & TV",
    "music": "music",
    "book": "books",
    "product": "products to shop for",
}

# Preference keys that are control metadata or non-text structures — excluded
# from the domain-neutral retry enrichment so an ambiguous query is never coerced.
_PREFERENCE_TERM_SKIP_KEYS = {"domain", "query", "confidence", "budget_range", "food_intent"}

_DOMAIN_ENTITY_KEYS: Dict[str, set[str]] = {
    "music": {"artist", "artists"},
    "movie": {"actors", "actor", "directors", "director", "with_cast", "with_crew"},
    "book": {"author", "authors", "publisher", "publishers"},
    "product": {"brand", "brands", "category", "categories"},
    # `location` is shared with restaurants, so only hotel-specific stay keys hint.
    "hotel": {"stars", "amenities"},
    # `attraction_types` (not `categories`) so product's category keys never collide.
    "attraction": {"attraction_types"},
}


def supported_domains() -> List[str]:
    return sorted(EXECUTABLE_DOMAINS)


def supported_domains_phrase() -> str:
    """A friendly, comma-joined phrase of the supported domains, e.g.
    'books, movies & TV, music, products to shop for, or restaurants'."""
    labels = [EXECUTABLE_DOMAIN_LABELS.get(domain, domain) for domain in supported_domains()]
    if not labels:
        return "recommendations"
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} or {labels[1]}"
    return ", ".join(labels[:-1]) + f", or {labels[-1]}"


def _meaningful_preference_terms(preferences: Optional[Dict[str, Any]]) -> List[str]:
    """Domain-neutral text terms from preferences, used only as extra context for
    re-classification. Carries no domain bias — restaurant prefs no longer force a
    restaurant route on an ambiguous query."""
    if not isinstance(preferences, dict):
        return []
    terms: List[str] = []
    for key, value in preferences.items():
        if key in _PREFERENCE_TERM_SKIP_KEYS:
            continue
        if isinstance(value, (list, tuple, set)):
            terms.extend(str(item) for item in value if item and str(item).strip().lower() != "any")
        elif value and str(value).strip().lower() != "any":
            terms.append(str(value))
    return terms


def _has_meaningful_value(value: Any) -> bool:
    if isinstance(value, (list, tuple, set)):
        return any(_has_meaningful_value(item) for item in value)
    if isinstance(value, dict):
        return any(_has_meaningful_value(item) for item in value.values())
    return value not in (None, "", [], {}, "any")


def _preference_domain_hint(preferences: Optional[Dict[str, Any]]) -> Optional[tuple[str, float, str]]:
    """Domain evidence extracted from LLM-structured preferences.

    This is intentionally separate from keyword scoring: the LLM is responsible
    for semantic parsing, while keywords are the fallback when the semantic frame
    is absent. Generic entity keys are strong hints because fields such as
    ``artist``/``author`` are domain-specific in the prompt and tool schemas.
    """
    if not isinstance(preferences, dict):
        return None

    structured_domains = _structured_preference_domains(preferences)
    explicit = str(preferences.get("domain") or "").strip().lower()
    if explicit == "multi_domain" and len(structured_domains) >= 2:
        return "multi_domain", 0.94, f"LLM preference domains: {structured_domains}"
    if len(structured_domains) >= 2:
        return "multi_domain", 0.9, f"LLM preference frame matched multiple domains: {structured_domains}"
    if explicit in EXECUTABLE_DOMAINS:
        return explicit, 0.92, f"LLM preference domain: {explicit}"

    entity_domains: list[str] = []
    for domain, keys in _DOMAIN_ENTITY_KEYS.items():
        if any(_has_meaningful_value(preferences.get(key)) for key in keys):
            entity_domains.append(domain)
    if len(entity_domains) == 1:
        domain = entity_domains[0]
        return domain, 0.88, f"LLM preference entities matched {domain}"
    if len(entity_domains) > 1:
        return "multi_domain", 0.8, f"LLM preference entities matched multiple domains: {entity_domains}"

    # Restaurant is more prone to stale profile/default leakage, so only use
    # explicit food intent as a semantic hint; generic restaurant prefs alone do
    # not coerce ambiguous queries into restaurants.
    food_intent = preferences.get("food_intent")
    if isinstance(food_intent, dict):
        cuisines = food_intent.get("cuisines") if isinstance(food_intent.get("cuisines"), list) else []
        dishes = food_intent.get("dishes") if isinstance(food_intent.get("dishes"), list) else []
        if cuisines or dishes:
            return "restaurant", 0.86, "LLM food intent matched restaurant"
    return None


@dataclass
class DomainRoute:
    domain: str
    mode: str
    status: str
    tool_tags: List[str]
    domain_confidence: float = 0.0
    reason: Optional[str] = None
    execution_domain: Optional[str] = None
    domain_tasks: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def can_execute(self) -> bool:
        return self.status == "ready" and bool(self.execution_domain)

    @property
    def is_restaurant_execution(self) -> bool:
        return self.can_execute and self.execution_domain == "restaurant"

    def to_payload(self, domain_lock: Optional[str] = None) -> Dict[str, Any]:
        """Canonical serialized routing dict. Single source of truth for the
        ``routing`` shape carried in response payloads and persisted HITL state."""
        payload = asdict(self)
        payload["domain_lock"] = domain_lock
        return payload


class RoutingRuntimeState(TypedDict, total=False):
    query: str
    intent: Optional[str]
    preferences: Optional[Dict[str, Any]]
    domain: str
    domain_confidence: float
    domain_reason: Optional[str]
    mode: str
    domain_lock: Optional[str]
    force_itinerary: bool
    route: DomainRoute
    errors: List[str]
    retry_count: int
    max_retries: int
    detected_domains: List[str]


# Itinerary is a *mode*, not a domain. Checked before keyword classification so
# "plan my day with museums and dinner" becomes one planning request rather
# than a multi-domain fan-out.
_ITINERARY_KEYWORDS = [
    "itinerary", "itineraries", "plan my day", "plan a day", "plan the day",
    "day trip", "day out", "one-day plan", "one day plan", "full day",
    "day plan", "trip plan", "half-day", "half day",
    "行程", "一日游", "一日遊", "一日行程", "半日游", "半日遊",
    "两日游", "兩日遊", "二日游", "规划一天", "規劃一天", "安排一天", "一天的行程",
]

_HOTEL_ORIGIN_RE = re.compile(
    r"(?:\bfrom\s+(?:my\s+|the\s+)?hotel\b|\bstart(?:ing)?\s+(?:at|from)\s+.*hotel\b|从.{0,30}(?:酒店|旅馆|旅店)出发|(?:酒店|旅馆|旅店)出发)",
    re.IGNORECASE,
)


def _is_itinerary_query(query: str) -> bool:
    return _keyword_score(query or "", _ITINERARY_KEYWORDS) > 0


_MULTI_DOMAIN_CONNECTOR_RE = re.compile(
    r"(?:\b(?:and|plus|along\s+with|as\s+well\s+as)\b|[,&+/]|(?:和|与|及|以及|还有|并且|、))",
    re.IGNORECASE,
)


def _structured_preference_domains(preferences: Optional[Dict[str, Any]]) -> List[str]:
    """Executable domains explicitly represented by the LLM preference frame."""
    if not isinstance(preferences, dict):
        return []
    domains: List[str] = []
    declared = preferences.get("domains")
    if isinstance(declared, str):
        declared = [part.strip() for part in declared.split(",")]
    if isinstance(declared, (list, tuple, set)):
        for value in declared:
            domain = str(value or "").strip().lower()
            if domain in EXECUTABLE_DOMAINS and domain not in domains:
                domains.append(domain)
    explicit = str(preferences.get("domain") or "").strip().lower()
    if explicit in EXECUTABLE_DOMAINS and explicit not in domains:
        domains.append(explicit)
    for domain, keys in _DOMAIN_ENTITY_KEYS.items():
        if any(_has_meaningful_value(preferences.get(key)) for key in keys) and domain not in domains:
            domains.append(domain)
    return domains


def _explicit_query_domains(query: str) -> List[str]:
    """Multiple keyword domains only when the query also coordinates them.

    This lets a legacy single-domain LLM frame recover requests such as
    "a hotel and attractions" without treating "attractions near my hotel" as
    two recommendation tasks.
    """
    domains = detect_domains(query)
    if len(domains) < 2 or not _MULTI_DOMAIN_CONNECTOR_RE.search(query or ""):
        return []
    return domains


def tool_tags_for_domain(domain: str) -> List[str]:
    return [normalize_tag(tag) for tag in DOMAIN_TOOL_TAGS.get(domain, [])]


def normalize_domain_lock(domain_lock: Optional[str]) -> Optional[str]:
    value = str(domain_lock or "").strip().lower()
    if not value or value == "auto":
        return None
    return value if value in SUPPORTED_DOMAIN_LOCKS else None


def _future_domain_route(domain: str, confidence: float, reason: str) -> DomainRoute:
    return DomainRoute(
        domain=domain,
        execution_domain=None,
        mode="single_domain",
        status="future_domain",
        tool_tags=tool_tags_for_domain(domain),
        domain_confidence=confidence,
        reason=reason,
        domain_tasks=[
            {
                "domain": domain,
                "status": "future_domain",
                "tool_tags": tool_tags_for_domain(domain),
            }
        ],
    )


def _ready_domain_task(domain: str, source_domain: Optional[str] = None) -> Dict[str, Any]:
    return {
        "domain": domain,
        "source_domain": source_domain or domain,
        "status": "ready",
        "tool_tags": tool_tags_for_domain(domain),
    }


def _domain_error_route(confidence: float, reason: str, retry_count: int) -> DomainRoute:
    return DomainRoute(
        domain="unknown",
        execution_domain=None,
        mode="domain_error",
        status="domain_error",
        tool_tags=[],
        domain_confidence=confidence,
        reason=reason,
        domain_tasks=[],
        metadata={
            "retry_count": retry_count,
            "clarification_required": True,
            "next_node": "collect_confirm_preferences",
        },
    )


def build_routing_graph():
    def domain_classification(runtime_state: RoutingRuntimeState) -> RoutingRuntimeState:
        locked_domain = normalize_domain_lock(runtime_state.get("domain_lock"))
        if locked_domain:
            return {
                **runtime_state,
                "domain": locked_domain,
                "domain_confidence": 1.0,
                "domain_reason": f"domain locked by service type: {locked_domain}",
                "mode": "single_domain",
            }

        query = runtime_state.get("query", "")
        state_preferences = runtime_state.get("preferences")
        force_itinerary = bool(runtime_state.get("force_itinerary"))
        llm_itinerary = (
            isinstance(state_preferences, dict)
            and str(state_preferences.get("domain") or "").strip().lower() == "itinerary"
        )
        if force_itinerary or _is_itinerary_query(query) or llm_itinerary:
            if force_itinerary:
                reason, confidence = "itinerary mode enabled by user", 1.0
            elif _is_itinerary_query(query):
                reason, confidence = "itinerary keywords matched", 0.9
            else:
                reason, confidence = "LLM preference domain: itinerary", 0.92
            return {
                **runtime_state,
                "domain": "itinerary",
                "domain_confidence": confidence,
                "domain_reason": reason,
                "mode": "itinerary",
            }

        query_domains = _explicit_query_domains(query)
        preference_hint = _preference_domain_hint(runtime_state.get("preferences"))
        if preference_hint:
            domain, confidence, reason = preference_hint
            if domain != "multi_domain" and domain in query_domains:
                domain = "multi_domain"
                confidence = max(confidence, 0.9)
                reason = f"explicit multi-domain query matched: {query_domains}; {reason}"
            return {
                **runtime_state,
                "domain": domain,
                "domain_confidence": confidence,
                "domain_reason": reason,
                "mode": "multi_domain" if domain == "multi_domain" else "single_domain",
                "detected_domains": query_domains or _structured_preference_domains(runtime_state.get("preferences")),
            }

        domain, confidence, reason = classify_domain(query)
        return {
            **runtime_state,
            "domain": domain,
            "domain_confidence": confidence,
            "domain_reason": reason,
            "mode": "multi_domain" if domain == "multi_domain" else "single_domain",
            "detected_domains": detect_domains(query) if domain == "multi_domain" else [],
        }

    def route_after_classification(runtime_state: RoutingRuntimeState) -> str:
        if runtime_state.get("domain") == "unknown":
            return "retry_domain"
        if runtime_state.get("mode") == "itinerary":
            return "itinerary"
        return "multi_domain" if runtime_state.get("mode") == "multi_domain" else "single_domain"

    def retry_domain(runtime_state: RoutingRuntimeState) -> RoutingRuntimeState:
        retry_count = int(runtime_state.get("retry_count", 0)) + 1
        # Re-classify the query with any domain-neutral preference text as extra
        # context. No domain is injected, so an ambiguous query that no registered
        # domain matches stays unknown (-> graceful "what we support" reply) rather
        # than being coerced into restaurant.
        preference_terms = _meaningful_preference_terms(runtime_state.get("preferences"))
        retry_query = " ".join([runtime_state.get("query", ""), *preference_terms]).strip()
        domain, confidence, reason = classify_domain(retry_query)
        if domain == "unknown":
            return {
                **runtime_state,
                "retry_count": retry_count,
                "domain": domain,
                "domain_confidence": confidence,
                "domain_reason": f"domain retry failed: {reason}",
                "mode": "domain_error",
            }
        return {
            **runtime_state,
            "retry_count": retry_count,
            "domain": domain,
            "domain_confidence": confidence,
            "domain_reason": f"domain retry matched after preference enrichment: {reason}",
            "mode": "multi_domain" if domain == "multi_domain" else "single_domain",
        }

    def route_after_retry(runtime_state: RoutingRuntimeState) -> str:
        if runtime_state.get("mode") == "domain_error":
            return "domain_error"
        return "multi_domain" if runtime_state.get("mode") == "multi_domain" else "single_domain"

    def single_domain(runtime_state: RoutingRuntimeState) -> RoutingRuntimeState:
        domain = runtime_state.get("domain", "unknown")
        confidence = float(runtime_state.get("domain_confidence", 0.0))
        reason = runtime_state.get("domain_reason")

        if domain in EXECUTABLE_DOMAINS:
            execution_domain = domain
            route = DomainRoute(
                domain=domain,
                execution_domain=execution_domain,
                mode="single_domain",
                status="ready",
                tool_tags=tool_tags_for_domain(execution_domain),
                domain_confidence=confidence,
                reason=reason or f"{domain}-compatible route",
                domain_tasks=[_ready_domain_task(execution_domain, domain)],
            )
        else:
            route = _future_domain_route(domain, confidence, reason or f"{domain} domain is not connected")

        return {**runtime_state, "route": route}

    def domain_error(runtime_state: RoutingRuntimeState) -> RoutingRuntimeState:
        route = _domain_error_route(
            float(runtime_state.get("domain_confidence", 0.0)),
            runtime_state.get("domain_reason") or "domain could not be classified",
            int(runtime_state.get("retry_count", 0)),
        )
        return {**runtime_state, "route": route}

    def multi_domain(runtime_state: RoutingRuntimeState) -> RoutingRuntimeState:
        confidence = float(runtime_state.get("domain_confidence", 0.0))
        reason = runtime_state.get("domain_reason") or "multi-domain request detected"
        domains = list(runtime_state.get("detected_domains") or detect_domains(runtime_state.get("query", "")))
        for domain in _structured_preference_domains(runtime_state.get("preferences")):
            if domain not in domains:
                domains.append(domain)
        tasks: List[Dict[str, Any]] = []
        tool_tags: List[str] = []
        for domain in domains:
            tags = tool_tags_for_domain(domain)
            if domain in EXECUTABLE_DOMAINS:
                tasks.append(_ready_domain_task(domain))
                tool_tags.extend(tags)
            else:
                tasks.append(
                    {
                        "domain": domain,
                        "status": "future_domain",
                        "tool_tags": tags,
                    }
                )
        has_ready_task = any(task.get("status") == "ready" for task in tasks)
        route = DomainRoute(
            domain="multi_domain",
            execution_domain="multi_domain" if has_ready_task else None,
            mode="multi_domain",
            status="ready" if has_ready_task else "future_multi_domain",
            tool_tags=sorted(set(tool_tags)),
            domain_confidence=confidence,
            reason=reason,
            domain_tasks=tasks,
            metadata={
                "decomposition_status": "ready" if has_ready_task else "future_multi_domain",
                "ready_domains": [task["domain"] for task in tasks if task.get("status") == "ready"],
                "future_domains": [task["domain"] for task in tasks if task.get("status") != "ready"],
            },
        )
        return {**runtime_state, "route": route}

    def itinerary(runtime_state: RoutingRuntimeState) -> RoutingRuntimeState:
        itinerary_query = runtime_state.get("query", "")
        route = DomainRoute(
            domain="itinerary",
            execution_domain="itinerary",
            mode="itinerary",
            status="ready",
            tool_tags=[],
            domain_confidence=float(runtime_state.get("domain_confidence", 0.9)),
            reason=runtime_state.get("domain_reason") or "itinerary request",
            domain_tasks=[],
            metadata={
                "planning_phase": "constraints_pending",
                "hotel_anchor_requested": bool(_HOTEL_ORIGIN_RE.search(itinerary_query)),
            },
        )
        return {**runtime_state, "route": route}

    graph = StateGraph(RoutingRuntimeState)
    graph.add_node("domain_classification", domain_classification)
    graph.add_node("retry_domain", retry_domain)
    graph.add_node("single_domain", single_domain)
    graph.add_node("multi_domain", multi_domain)
    graph.add_node("itinerary", itinerary)
    graph.add_node("domain_error", domain_error)
    graph.add_edge(START, "domain_classification")
    graph.add_conditional_edges(
        "domain_classification",
        route_after_classification,
        {
            "retry_domain": "retry_domain",
            "single_domain": "single_domain",
            "multi_domain": "multi_domain",
            "itinerary": "itinerary",
        },
    )
    graph.add_conditional_edges(
        "retry_domain",
        route_after_retry,
        {"domain_error": "domain_error", "single_domain": "single_domain", "multi_domain": "multi_domain"},
    )
    graph.add_edge("single_domain", END)
    graph.add_edge("multi_domain", END)
    graph.add_edge("itinerary", END)
    graph.add_edge("domain_error", END)
    return graph.compile()


async def run_routing_graph(
    *,
    query: str,
    intent: Optional[str] = None,
    preferences: Optional[Dict[str, Any]] = None,
    domain_lock: Optional[str] = None,
    force_itinerary: bool = False,
    trace_ctx: Optional[TraceContext] = None,
) -> DomainRoute:
    graph = build_routing_graph()
    final_state = await invoke_with_wp8_trace(
        graph,
        {
            "query": query,
            "intent": intent,
            "preferences": preferences,
            "domain_lock": normalize_domain_lock(domain_lock),
            # A service-type lock is an explicit single-domain intent, so it wins
            # over the itinerary switch (the UI keeps them mutually exclusive).
            "force_itinerary": force_itinerary,
            "errors": [],
            "retry_count": 0,
            "max_retries": 1,
        },
        None,
        trace_ctx,
        graph_definition_id="routing-graph@v1",
    )
    route = final_state.get("route")
    if route is None:
        raise RuntimeError("Routing graph finished without a route")
    return route
