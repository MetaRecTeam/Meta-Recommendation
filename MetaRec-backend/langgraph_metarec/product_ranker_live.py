"""Live product ranking for MetaRec: M2 wired into the request path. WP4 D4.

The D3 module (``product_ranker_m2``) scores dataset cards for the offline study. This
module runs the same six components over the candidates of a real request, after the
provider output has been normalised and de-duplicated and before the top-10 cut:

  * ``eligibility``     the hard-constraint gate and the missing-price policy of D3;
  * ``text_relevance``  BM25 between the current request and each candidate's text;
  * ``attribute_match`` how many of the stated attributes (brand, category, use case,
                        model) a candidate's text contains;
  * ``collaborative``   similarity to what the signed-in user saved or bought, below
                        every other score for what they hid; zero when there is no
                        history (cold start);
  * ``popularity``      ln(1 + review count);
  * ``tail_adjustment`` a boost for candidates below the pool's head threshold.

It only reorders or removes the candidates it is given, joins the result back to the
untouched internal items, and writes a deterministic sentence to the existing ``why``
field. Scores and components stay in the server-side experiment metadata and never
reach a public field. Any failure falls back to the original order (still passing the
hard gate), and the legacy flag setting never reaches this module at all.
"""
from __future__ import annotations

import math
import os
import re
import statistics
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field

from langgraph_metarec.product_constraints import ProductConstraints
from langgraph_metarec.product_ranker_m2 import (
    ATTRIBUTE_MATCH_THRESHOLD,
    HISTORY_AFFINITY_ZSCORE_THRESHOLD,
    REASON_CODE_ALLOW_LIST,
    USE_CASE_MATCH_ZSCORE_THRESHOLD,
    M2Score,
    RankerResult,
    _candidate_price,
    exclusion_reason_codes,
    rank_candidates,
    tokenize,
    zscore,
)

# ---------------------------------------------------------------------------
# The flag
# ---------------------------------------------------------------------------

PRODUCT_RANKER_ENV = "METAREC_PRODUCT_RANKER"
PRODUCT_RANKER_MODES = ("legacy", "domain_v1")
RANKER_TIMEOUT_ENV = "METAREC_PRODUCT_RANKER_TIMEOUT_S"
DEFAULT_RANKER_TIMEOUT_S = 1.0


def validate_product_ranker_setting(env: Optional[Dict[str, str]] = None) -> str:
    """The configured mode, or ``ValueError`` for anything but exactly ``legacy`` or
    ``domain_v1``. Unset or empty means ``legacy``. Called once at startup, so a typo
    stops the service instead of silently running the wrong ranker."""
    source = os.environ if env is None else env
    raw = source.get(PRODUCT_RANKER_ENV)
    if raw is None or raw == "":
        return "legacy"
    if raw not in PRODUCT_RANKER_MODES:
        raise ValueError(
            f"{PRODUCT_RANKER_ENV} must be exactly one of {', '.join(PRODUCT_RANKER_MODES)}; got {raw!r}"
        )
    return raw


def ranker_timeout_seconds(env: Optional[Dict[str, str]] = None) -> float:
    source = os.environ if env is None else env
    try:
        value = float(source.get(RANKER_TIMEOUT_ENV, DEFAULT_RANKER_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_RANKER_TIMEOUT_S
    return value if value > 0 else DEFAULT_RANKER_TIMEOUT_S


# ---------------------------------------------------------------------------
# Contracts (RankerRequestV1 / RankerResultV1 of the proposal)
# ---------------------------------------------------------------------------

MODEL_NAME = "m2-constraint-aware-hybrid"
MODEL_VERSION = "m2.live.1"
FEATURE_VERSION = "features.live.1"

# The weights tuned on the development data (interim report, section VIII-B).
LIVE_WEIGHTS: Dict[str, float] = {
    "text_relevance": 0.25,
    "attribute_match": 0.0,
    "collaborative": 0.05,
    "popularity": 0.65,
    "tail_adjustment": 0.05,
}

# Which exclusion code is reported as the single ``reason_code`` of an excluded item.
_EXCLUSION_PRECEDENCE = (
    "UNKNOWN_PRICE_STRICT_MODE",
    "OVER_BUDGET",
    "WRONG_BRAND",
    "WRONG_CATEGORY",
    "WRONG_MODEL",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RankerRequestV1(_Strict):
    schema_version: str = Field(default="ranker-request.v1", pattern=r"^ranker-request\.v1$")
    request_id: str
    task_id: Optional[str] = None
    domain: str = Field(default="product", pattern=r"^product$")
    query: str
    fused_preferences: Dict[str, Any] = Field(default_factory=dict)
    candidates: List[Dict[str, Any]]
    interactions: List[Dict[str, Any]] = Field(default_factory=list)
    top_k: int = Field(default=10, ge=1, le=50)
    seed: int = 0


class RankedEntryV1(_Strict):
    item_id: str
    rank: int = Field(ge=1)
    score: float
    score_components: Dict[str, float]
    reason_codes: List[str]


class ExcludedEntryV1(_Strict):
    item_id: str
    reason_code: str


class RankerResultV1(_Strict):
    schema_version: str = Field(default="ranker-result.v1", pattern=r"^ranker-result\.v1$")
    request_id: str
    ranked: List[RankedEntryV1]
    excluded: List[ExcludedEntryV1]
    model_name: str = MODEL_NAME
    model_version: str = MODEL_VERSION
    feature_version: str = FEATURE_VERSION
    warnings: List[str] = Field(default_factory=list)
    latency_ms: float = Field(ge=0)


# ---------------------------------------------------------------------------
# Identity: ASIN first
# ---------------------------------------------------------------------------

_ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?#]|$)")
_ASIN_ONLY = re.compile(r"^[A-Z0-9]{10}$")


def asin_from_link(link: Any) -> Optional[str]:
    match = _ASIN_RE.search(str(link or ""))
    return match.group(1) if match else None


def asin_of(raw: Dict[str, Any]) -> Optional[str]:
    """ASIN of a provider result: its own ``asin`` field when valid, else the one in the link."""
    own = str(raw.get("asin") or "").strip().upper()
    if _ASIN_ONLY.match(own):
        return own
    return asin_from_link(raw.get("link"))


# ---------------------------------------------------------------------------
# History: the read adapter over the item-interaction seam
# ---------------------------------------------------------------------------

POSITIVE_ACTIONS = frozenset({"save", "positive", "consumed"})
NEGATIVE_ACTIONS = frozenset({"hide", "negative"})


@dataclass
class HistoryEvent:
    action: str
    item_id: str
    title: str = ""
    asin: Optional[str] = None


def history_from_records(records: Iterable[Any]) -> Tuple[List[Dict[str, Any]], List[HistoryEvent]]:
    """``ItemInteractionV1`` dicts (the wire shape) and the content events (snapshot title,
    ASIN from the snapshot link) for a user's product interactions, oldest first.

    ``records`` are ``ItemInteractionRecord`` objects from
    ``item_interaction_repository.list_for_user``. Repeats are kept, since a second
    "Purchased" is a second event."""
    from business_models import to_interaction_v1

    wire: List[Dict[str, Any]] = []
    events: List[HistoryEvent] = []
    for record in records or []:
        wire.append(to_interaction_v1(record))
        snapshot = (getattr(record, "payload", None) or {}).get("item") or {}
        events.append(
            HistoryEvent(
                action=str(record.action),
                item_id=str(record.item_id),
                title=str(snapshot.get("title") or ""),
                asin=asin_from_link(snapshot.get("url")),
            )
        )
    return wire, events


# ---------------------------------------------------------------------------
# Scoring the live candidates
# ---------------------------------------------------------------------------

_ATTRIBUTE_KEYS = ("product", "brand", "category", "use_case", "model")
_TOKEN_STOP = frozenset({"a", "an", "the", "for", "and", "or", "of", "to", "in", "with", "under", "below", "usd", "sgd"})


@dataclass
class LiveContext:
    query: str
    preferences: Dict[str, Any]
    events: List[HistoryEvent] = field(default_factory=list)
    weights: Dict[str, float] = field(default_factory=lambda: dict(LIVE_WEIGHTS))


def candidate_id(item: Dict[str, Any], prefer_asin: bool = True) -> str:
    """The id M2 works with: the ASIN when the item has one, else the item's own id."""
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    asin = asin_of(raw) if prefer_asin else None
    return asin or str(item["id"])


def _candidate_text(item: Dict[str, Any]) -> str:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    parts = [item.get("title") or "", raw.get("brand") or "", raw.get("category") or "", raw.get("type") or ""]
    return " ".join(str(p) for p in parts if p)


def _attribute_tokens(preferences: Dict[str, Any]) -> List[str]:
    seen: Dict[str, None] = {}
    for key in _ATTRIBUTE_KEYS:
        value = (preferences or {}).get(key)
        if isinstance(value, (list, tuple)):
            value = " ".join(str(v) for v in value)
        for token in tokenize(str(value or "")):
            if token not in _TOKEN_STOP and len(token) > 1:
                seen.setdefault(token, None)
    return list(seen)


def _query_tokens(ctx: LiveContext) -> List[str]:
    tokens = [t for t in tokenize(ctx.query) if t not in _TOKEN_STOP]
    extra = [t for t in _attribute_tokens(ctx.preferences) if t not in set(tokens)]
    return tokens + extra


def _bm25(query_tokens: List[str], texts: List[str]) -> List[float]:
    from rank_bm25 import BM25Okapi

    corpus = [tokenize(t) for t in texts]
    if not query_tokens or not any(corpus):
        return [0.0] * len(texts)
    return list(BM25Okapi(corpus).get_scores(query_tokens))


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _affinity_raw(candidates: List[Dict[str, Any]], ctx: LiveContext) -> List[float]:
    positives = [e for e in ctx.events if e.action in POSITIVE_ACTIONS]
    negatives = [e for e in ctx.events if e.action in NEGATIVE_ACTIONS]
    pos_asins = {e.asin for e in positives if e.asin}
    neg_asins = {e.asin for e in negatives if e.asin}
    pos_tokens = [set(tokenize(e.title)) for e in positives if e.title]
    raw: List[float] = []
    for cand in candidates:
        asin = asin_of(cand.get("raw") if isinstance(cand.get("raw"), dict) else {})
        if asin and asin in neg_asins and asin not in pos_asins:
            raw.append(-1.0)
        elif asin and asin in pos_asins:
            raw.append(1.0)
        else:
            tokens = set(tokenize(_candidate_text(cand)))
            raw.append(max((_jaccard(tokens, p) for p in pos_tokens), default=0.0))
    return raw


def _head_threshold(counts: List[int]) -> int:
    """Review count at the 80th percentile of the pool: items below it are tail."""
    known = sorted(c for c in counts if c)
    if not known:
        return 1
    index = max(0, math.ceil(0.8 * len(known)) - 1)
    return known[index]


def make_live_scorer(ctx: LiveContext) -> Callable[..., List[M2Score]]:
    """A scorer with the signature ``rank_candidates`` expects. It accepts the offline
    keyword arguments and ignores the ones that only make sense for dataset cards."""

    def score_live(*, candidates: List[Dict[str, Any]], constraints: Optional[ProductConstraints] = None,
                   weights: Optional[Dict[str, float]] = None, **_ignored: Any) -> List[M2Score]:
        w = weights or ctx.weights
        c = constraints or ProductConstraints()
        has_history = any(e.action in POSITIVE_ACTIONS | NEGATIVE_ACTIONS for e in ctx.events)

        exclusion: List[List[str]] = [[] for _ in candidates] if c.is_empty() else [
            exclusion_reason_codes(cand, c) for cand in candidates
        ]
        eligible_idx = [i for i, codes in enumerate(exclusion) if not codes]
        eligible = [candidates[i] for i in eligible_idx]
        position = {i: k for k, i in enumerate(eligible_idx)}

        if eligible:
            text_z = zscore(_bm25(_query_tokens(ctx), [_candidate_text(x) for x in eligible]))
            attr_tokens = _attribute_tokens(ctx.preferences)
            attr_raw = []
            for x in eligible:
                tokens = set(tokenize(_candidate_text(x)))
                attr_raw.append(sum(1 for t in attr_tokens if t in tokens) / len(attr_tokens) if attr_tokens else 0.0)
            attr_z = zscore(attr_raw)
            collab_z = zscore(_affinity_raw(eligible, ctx)) if has_history else [0.0] * len(eligible)
            counts = [int(x.get("reviews_count") or 0) for x in eligible]
            pop_z = zscore([math.log1p(n) for n in counts])
            head = _head_threshold(counts)
            tail_z = zscore([0.0 if n >= head else 1.0 for n in counts])
        else:
            text_z = attr_z = collab_z = pop_z = tail_z = []

        results: List[M2Score] = []
        for i, cand in enumerate(candidates):
            cid = cand["parent_asin"]
            if exclusion[i]:
                results.append(M2Score(item_id=cid, score=float("-inf"), score_components={}, excluded=True,
                                       exclusion_reason_codes=exclusion[i]))
                continue
            k = position[i]
            components = {
                "eligibility": 1.0,
                "text_relevance": text_z[k],
                "attribute_match": attr_z[k],
                "collaborative": collab_z[k],
                "popularity": pop_z[k],
                "tail_adjustment": tail_z[k],
            }
            score = sum(w.get(name, 0.0) * v for name, v in components.items() if name != "eligibility")
            codes: List[str] = []
            if not has_history:
                codes.append("POPULAR_FALLBACK")
            else:
                if collab_z[k] > HISTORY_AFFINITY_ZSCORE_THRESHOLD:
                    codes.append("HISTORY_AFFINITY")
            if text_z[k] > USE_CASE_MATCH_ZSCORE_THRESHOLD:
                codes.append("USE_CASE_MATCH")
            if attr_z[k] > ATTRIBUTE_MATCH_THRESHOLD:
                codes.append("ATTRIBUTE_MATCH")
            flags = ["PRICE_MISSING"] if _candidate_price(cand) is None else []
            results.append(M2Score(item_id=cid, score=score, score_components=components,
                                   reason_codes=codes, flags=flags))
        return results

    return score_live


# ---------------------------------------------------------------------------
# The sentence for the existing ``why`` field
# ---------------------------------------------------------------------------

_WHY_PHRASES = {
    "USE_CASE_MATCH": "closely matches your request",
    "ATTRIBUTE_MATCH": "matches the brand, category or use you stated",
    "HISTORY_AFFINITY": "similar to items you saved or bought",
}


def deterministic_why(item: Dict[str, Any], reason_codes: Sequence[str], constraints: ProductConstraints) -> str:
    """A fixed template over fields the candidate really has. Nothing here is generated
    by a model, so it cannot claim anything the provider did not return."""
    parts = [_WHY_PHRASES[c] for c in reason_codes if c in _WHY_PHRASES]
    reviews = item.get("reviews_count")
    if "POPULAR_FALLBACK" in reason_codes and reviews:
        parts.append(f"popular choice with {reviews:,} reviews")
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    price = raw.get("price")
    if constraints.budget_max is not None and price and _candidate_price(item) is not None:
        parts.append(f"within your budget at {price}")
    if not parts:
        return "Meets the constraints you stated." if not constraints.is_empty() else "Matched the product search query."
    sentence = "; ".join(parts)
    return sentence[0].upper() + sentence[1:] + "."


# ---------------------------------------------------------------------------
# The entry point used by the graph
# ---------------------------------------------------------------------------


@dataclass
class LiveRankOutcome:
    items: List[Dict[str, Any]]           # reordered / filtered internal items (copies only differ in ``why``)
    result: RankerResultV1                 # the contract result, for server metadata
    excluded_codes: Dict[str, List[str]]   # every reason code per excluded id
    fell_back: bool
    exhausted: bool                        # the gate removed every candidate


def _contract_result(request_id: str, ranker_result: RankerResult, id_of: Callable[[str], str]) -> RankerResultV1:
    ranked = [
        RankedEntryV1(
            item_id=id_of(r.item_id),
            rank=r.rank,
            score=float(r.score) if math.isfinite(r.score) else 0.0,
            score_components={k: float(v) for k, v in r.score_components.items()},
            reason_codes=[c for c in r.reason_codes if c in REASON_CODE_ALLOW_LIST],
        )
        for r in ranker_result.ranked
    ]
    excluded = []
    for e in ranker_result.excluded:
        code = next((c for c in _EXCLUSION_PRECEDENCE if c in e.reason_codes), e.reason_codes[0] if e.reason_codes else "WRONG_MODEL")
        excluded.append(ExcludedEntryV1(item_id=id_of(e.item_id), reason_code=code))
    return RankerResultV1(
        request_id=request_id,
        ranked=ranked,
        excluded=excluded,
        warnings=list(ranker_result.warnings),
        latency_ms=ranker_result.latency_ms,
    )


def rank_live(
    *,
    items: List[Dict[str, Any]],
    query: str,
    preferences: Dict[str, Any],
    constraints: ProductConstraints,
    interactions: Optional[Sequence[Dict[str, Any]]] = None,
    events: Optional[Sequence[HistoryEvent]] = None,
    timeout_s: Optional[float] = None,
    scorer: Optional[Callable[..., List[M2Score]]] = None,
    request_id: Optional[str] = None,
    task_id: Optional[str] = None,
    top_k: int = 10,
    seed: int = 0,
) -> LiveRankOutcome:
    """Rank the live candidates with M2, or fall back to their original order.

    ``items`` are the internal items in B0 order (after normalisation and de-duplication).
    The ids M2 sees are ASINs where the provider result carries one. An error, a timeout,
    an empty output, or a duplicate / unknown / missing id returns the original order with
    a ``FALLBACK_*`` warning, still passing the hard-constraint gate so that a failure can
    never put a violator back in front of the user."""
    request_id = request_id or uuid.uuid4().hex
    RankerRequestV1(  # validates the request against the contract; unknown fields are rejected
        request_id=request_id,
        task_id=task_id,
        query=query or "",
        fused_preferences=dict(preferences or {}),
        candidates=[dict(i) for i in items],
        interactions=list(interactions or []),
        top_k=top_k,
        seed=seed,
    )
    by_cid: Dict[str, Dict[str, Any]] = {}
    candidates: List[Dict[str, Any]] = []
    for item in items:
        cid = candidate_id(item)
        if cid in by_cid:
            continue  # the same ASIN twice: keep the first, which is the one B0 ranked higher
        by_cid[cid] = item
        candidates.append({**item, "parent_asin": cid})
    ctx = LiveContext(query=query or "", preferences=dict(preferences or {}), events=list(events or []))
    result = rank_candidates(
        candidates=candidates,
        history_asins=[],
        history_categories=set(),
        history_brands=set(),
        items={},
        item_users={},
        head_threshold=0,
        constraints=constraints,
        weights=ctx.weights,
        timeout_s=ranker_timeout_seconds() if timeout_s is None else timeout_s,
        scorer=scorer or make_live_scorer(ctx),
    )

    has_history = any(e.action in POSITIVE_ACTIONS | NEGATIVE_ACTIONS for e in (events or []))
    result.warnings = [w for w in result.warnings if w != "COLD_START"] + ([] if has_history else ["COLD_START"])
    excluded_codes = {e.item_id: list(e.reason_codes) for e in result.excluded}
    fell_back = result.fell_back
    if fell_back:
        # the contract says original order; the gate still applies
        kept, excluded_codes = [], {}
        for cand in candidates:
            codes = [] if constraints.is_empty() else exclusion_reason_codes(cand, constraints)
            if codes:
                excluded_codes[cand["parent_asin"]] = codes
            else:
                kept.append(cand["parent_asin"])
        ordered_ids = kept
        result = RankerResult(
            ranked=[], excluded=[], warnings=result.warnings, latency_ms=result.latency_ms, fell_back=True
        )
        reasons: Dict[str, List[str]] = {}
    else:
        ordered_ids = [r.item_id for r in result.ranked]
        reasons = {r.item_id: list(r.reason_codes) for r in result.ranked}

    out_items: List[Dict[str, Any]] = []
    for cid in ordered_ids:
        original = by_cid[cid]
        if fell_back:
            out_items.append(original)
            continue
        copy = dict(original)
        copy["why"] = deterministic_why(original, reasons.get(cid, []), constraints)
        out_items.append(copy)

    contract = _contract_result(request_id, result, lambda x: x)
    if fell_back:
        contract = RankerResultV1(
            request_id=request_id, ranked=[], excluded=[
                ExcludedEntryV1(item_id=cid, reason_code=next((c for c in _EXCLUSION_PRECEDENCE if c in codes), codes[0]))
                for cid, codes in excluded_codes.items()
            ],
            warnings=list(result.warnings), latency_ms=result.latency_ms,
        )
    return LiveRankOutcome(
        items=out_items,
        result=contract,
        excluded_codes=excluded_codes,
        fell_back=fell_back,
        exhausted=bool(items) and not out_items and bool(excluded_codes),
    )
