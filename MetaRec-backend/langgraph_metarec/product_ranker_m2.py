"""M2 — constraint-aware hybrid ranker for the product domain. WP4 D3.

M2's definition (WP4-proposal-source.md, line 48): "Hard eligibility first,
then query/use-case and attribute relevance + authenticated-user affinity when
available + calibrated popularity/head-tail control."

Required score components (the proposal's RankerResultV1 contract, line 150):
``eligibility, text_relevance, attribute_match, collaborative, popularity,
tail_adjustment``. This module computes exactly these six, nothing invented.

Allow-listed reason codes (line 152-154): ``USE_CASE_MATCH, ATTRIBUTE_MATCH,
HISTORY_AFFINITY, POPULAR_FALLBACK, OVER_BUDGET, WRONG_BRAND, WRONG_CATEGORY,
WRONG_MODEL, UNKNOWN_PRICE_STRICT_MODE``. The hard-gate's 5 violation keys
(``product_constraints.violations``: budget/category/brand/compat/exclude)
map onto the 4 negative codes available for them — ``compat`` and ``exclude``
both represent an attribute/compatibility failure, so both map to
``WRONG_MODEL``; the proposal allow-lists no tenth code for them.

Order of work (the proposal's "hard eligibility first"):
  1. every candidate goes through the hard-constraint gate and the missing-price
     policy; violators are removed and keep their reason codes;
  2. the five ranking components are z-scored over the survivors only, so a
     removed candidate can never change how the survivors are scored;
  3. the weighted sum ranks the survivors.

Missing-price policy (Project Plan §6, "Missing price policy"): unknown is never
zero. When the request states a maximum budget, a candidate whose price is
unknown, or is in a different currency from the budget, cannot be compared and is
excluded with ``UNKNOWN_PRICE_STRICT_MODE``. With no budget stated it is kept and
the request carries a ``PRICE_MISSING`` warning.

"Authenticated-user affinity" (line 48) is explicitly conditional on live
history existing — and Yanny's correction says not to rely on live MetaRec
interaction data, since ``item_interactions`` starts empty. For this offline
evaluation on the frozen Amazon Reviews 2023 dataset, the honest stand-in is
the same collaborative (ItemKNN) signal already built for D2: a user's
training-history co-purchase pattern is the only affinity signal this dataset
can actually provide. ``HISTORY_AFFINITY`` is this component's reason code.

``rank_candidates`` wraps the scorer with the flag contract's fallback: an error,
a timeout, an empty output, or a duplicate / unknown / missing id returns the
candidates in their original order with a ``FALLBACK_*`` warning.
"""
from __future__ import annotations

import math
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from rank_bm25 import BM25Okapi

from langgraph_metarec.product_constraints import ProductConstraints, _price_of, parse_price, violations

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Fixed weights on the six required components (sum to 1.0 for interpretability
# — z-scoring already puts them on a comparable scale, so this isn't required,
# just tidy). Starting point reuses B1's tuned text/popularity balance
# (0.25 text : 0.75 popularity) and carves out room for the two new signals.
# The weights tuned on the development data are in the interim report, section 7.1.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "text_relevance": 0.25,
    "attribute_match": 0.10,
    "collaborative": 0.15,
    "popularity": 0.45,
    "tail_adjustment": 0.05,
}

# The proposal's reason-code allow-list (RankerResultV1, lines 151-154).
REASON_CODE_ALLOW_LIST = frozenset(
    {
        "USE_CASE_MATCH",
        "ATTRIBUTE_MATCH",
        "HISTORY_AFFINITY",
        "POPULAR_FALLBACK",
        "OVER_BUDGET",
        "WRONG_BRAND",
        "WRONG_CATEGORY",
        "WRONG_MODEL",
        "UNKNOWN_PRICE_STRICT_MODE",
    }
)

UNKNOWN_PRICE_CODE = "UNKNOWN_PRICE_STRICT_MODE"

# Negative (exclusion) reason codes, keyed by product_constraints.violations()'s
# own violation keys. "compat" and "exclude" both collapse onto WRONG_MODEL —
# documented above, not an oversight.
_VIOLATION_TO_REASON_CODE: Dict[str, str] = {
    "budget": "OVER_BUDGET",
    "brand": "WRONG_BRAND",
    "category": "WRONG_CATEGORY",
    "compat": "WRONG_MODEL",
    "exclude": "WRONG_MODEL",
}

USE_CASE_MATCH_ZSCORE_THRESHOLD = 0.0
ATTRIBUTE_MATCH_THRESHOLD = 0.0
HISTORY_AFFINITY_ZSCORE_THRESHOLD = 0.0


def tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


def zscore(values: List[float]) -> List[float]:
    if not values:
        return values
    mean = statistics.fmean(values)
    if len(values) < 2:
        return [0.0 for _ in values]
    stdev = statistics.pstdev(values)
    if stdev == 0:
        return [0.0 for _ in values]
    return [(v - mean) / stdev for v in values]


@dataclass
class M2Score:
    item_id: str
    score: float
    score_components: Dict[str, float]
    reason_codes: List[str] = field(default_factory=list)
    excluded: bool = False
    exclusion_reason_codes: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)  # e.g. PRICE_MISSING


def violation_keys_to_reason_codes(violation_keys: Sequence[str]) -> List[str]:
    codes = [_VIOLATION_TO_REASON_CODE[k] for k in violation_keys if k in _VIOLATION_TO_REASON_CODE]
    return list(dict.fromkeys(codes))  # de-duplicate, preserve order (compat+exclude can both fire WRONG_MODEL)


# ---------------------------------------------------------------------------
# Hard eligibility: the constraint gate and the missing-price policy
# ---------------------------------------------------------------------------


def _gate_view(cand: dict) -> dict:
    """The shape ``product_constraints.violations`` reads. A live item already has
    ``raw``; a dataset card keeps price, brand and category at the top level, so
    they are copied into a ``raw`` dict. Without this the gate cannot see a card's
    price or brand and would let a budget or brand violation through."""
    if isinstance(cand.get("raw"), dict):
        return cand
    view = dict(cand)
    view["raw"] = {
        "price": cand.get("price"),
        "brand": cand.get("brand"),
        "category": cand.get("main_category"),
    }
    return view


def _candidate_price(cand: dict) -> Optional[float]:
    """Numeric price, or None when it is unknown (never 0)."""
    if isinstance(cand.get("raw"), dict):
        return _price_of(cand)
    return parse_price(cand.get("price"))


def _candidate_currency(cand: dict) -> Optional[str]:
    raw = cand.get("raw") if isinstance(cand.get("raw"), dict) else {}
    cur = cand.get("currency") or raw.get("currency")
    return str(cur).strip().upper() if cur else None


def price_policy_code(cand: dict, constraints: ProductConstraints) -> Optional[str]:
    """``UNKNOWN_PRICE_STRICT_MODE`` when the request states a maximum budget and
    this candidate's price is unknown or in a different currency; otherwise None."""
    if constraints.budget_max is None:
        return None
    if _candidate_price(cand) is None:
        return UNKNOWN_PRICE_CODE
    cur = _candidate_currency(cand)
    if constraints.currency and cur and cur != constraints.currency.strip().upper():
        return UNKNOWN_PRICE_CODE
    return None


def exclusion_reason_codes(cand: dict, constraints: ProductConstraints) -> List[str]:
    """Reason codes for a candidate the gate removes; empty means eligible."""
    codes = violation_keys_to_reason_codes(violations(_gate_view(cand), constraints))
    policy = price_policy_code(cand, constraints)
    if policy:
        # A price that cannot be compared must not also be called over budget.
        codes = [c for c in codes if c != "OVER_BUDGET"] + [policy]
    return codes


# ---------------------------------------------------------------------------
# Ranking components
# ---------------------------------------------------------------------------


def _enriched_text(item_meta: dict) -> str:
    """title + brand + main_category + store, not title alone. Verified on a
    30K-case sample (warm users only) this beats title-only BM25 on that component;
    on the full development set the gain does not reach the final ranking (interim
    report, section 7.1). Titles are often marketing copy with inconsistent wording;
    brand/category/store are cleaner, more consistent fields."""
    parts = [
        item_meta.get("title") or "",
        item_meta.get("brand") or "",
        item_meta.get("main_category") or "",
        item_meta.get("store") or "",
    ]
    return " ".join(p for p in parts if p)


def text_relevance_scores(history_asins: List[str], candidates: List[dict], items: Dict[str, dict]) -> List[float]:
    """BM25 over enriched text (title+brand+category+store), both for the
    query (built from the user's history items) and for each candidate."""
    query_text = " ".join(_enriched_text(items.get(a, {})) for a in history_asins)
    query_tokens = tokenize(query_text)
    if not query_tokens or not candidates:
        return [0.0] * len(candidates)
    corpus = [tokenize(_enriched_text(c)) for c in candidates]
    if not any(corpus):  # BM25 divides by the average length
        return [0.0] * len(candidates)
    bm25 = BM25Okapi(corpus)
    raw = list(bm25.get_scores(query_tokens))
    return zscore(raw)


def attribute_match_scores(
    history_categories: Set[str], history_brands: Set[str], candidates: List[dict]
) -> List[float]:
    """Category/brand overlap with the user's history — a precise, coarse
    signal distinct from BM25's fuzzy title-word matching. Scored 0/0.5/1 per
    candidate (category match, brand match, or both), then z-scored like every
    other component so it combines on the same scale.

    This is a judgment call, not something the proposal specifies in formula
    form: for this offline dataset there is no explicit "required_attributes"
    field the way a real stated query would carry (e.g. "waterproof"), so
    category/brand overlap with the user's own past purchases is the closest
    honest proxy available from this data's actual fields.
    """
    raw = []
    for c in candidates:
        hit = 0.0
        if c.get("main_category") and c["main_category"] in history_categories:
            hit += 0.5
        if c.get("brand") and c["brand"] in history_brands:
            hit += 0.5
        raw.append(hit)
    return zscore(raw)


def tail_adjustment_scores(candidates: List[dict], head_threshold: int) -> List[float]:
    """1.0 for a tail item (rating_number < head_threshold), 0.0 for head —
    then z-scored. This is a boost toward catalogue coverage, counteracting
    popularity's inherent bias toward a small set of head items (Plan §6:
    top 20% by review count = head)."""
    raw = [0.0 if (c.get("rating_number") or 0) >= head_threshold else 1.0 for c in candidates]
    return zscore(raw)


def popularity_scores(candidates: List[dict]) -> List[float]:
    raw = [math.log1p(c.get("rating_number") or 0) for c in candidates]
    return zscore(raw)


def collaborative_scores(
    history_asins: List[str], candidates: List[dict], item_users: Dict[str, Set[str]]
) -> List[float]:
    """Same ItemKNN cosine-similarity logic from D2 — this IS the affinity
    signal for this offline dataset (see module docstring)."""
    if not history_asins:
        return [0.0] * len(candidates)
    history_user_sets = [item_users.get(a, set()) for a in history_asins]
    history_user_sets = [s for s in history_user_sets if s]
    if not history_user_sets:
        return [0.0] * len(candidates)

    def cosine(a: Set[str], b: Set[str]) -> float:
        if not a or not b:
            return 0.0
        inter = len(a & b)
        if inter == 0:
            return 0.0
        return inter / math.sqrt(len(a) * len(b))

    raw = []
    for c in candidates:
        c_users = item_users.get(c["parent_asin"], set())
        sims = [cosine(c_users, h_users) for h_users in history_user_sets]
        raw.append(statistics.fmean(sims))
    return zscore(raw)


def score_candidates(
    *,
    history_asins: List[str],
    history_categories: Set[str],
    history_brands: Set[str],
    candidates: List[dict],
    items: Dict[str, dict],
    item_users: Dict[str, Set[str]],
    head_threshold: int,
    constraints: Optional[ProductConstraints] = None,
    weights: Optional[Dict[str, float]] = None,
) -> List[M2Score]:
    """Full M2 hybrid score for every candidate, in input order: hard eligibility
    first, then the five ranking components over the survivors combined by `weights`.

    `constraints` defaults to empty (ProductConstraints() with every field
    unset), which is_empty() -> True, so the gate passes everything through —
    this offline dataset has no stated budget/brand/category per case (see
    module docstring), so eligibility is a structural no-op there. Its real
    test ground is the 15 labelled constraint fixtures from D0
    (tests/wp4/cases/), which DO carry real stated constraints.
    """
    weights = weights or DEFAULT_WEIGHTS
    c = constraints or ProductConstraints()
    is_cold = not history_asins

    # 1. hard eligibility first
    if c.is_empty():  # nothing stated, nothing to check (the whole offline Amazon evaluation)
        exclusion: List[List[str]] = [[] for _ in candidates]
    else:
        exclusion = [exclusion_reason_codes(cand, c) for cand in candidates]
    eligible_idx = [i for i, codes in enumerate(exclusion) if not codes]
    eligible = [candidates[i] for i in eligible_idx]

    # 2. components over the survivors only
    if eligible:
        text_z = text_relevance_scores(history_asins, eligible, items)
        attr_z = attribute_match_scores(history_categories, history_brands, eligible)
        collab_z = collaborative_scores(history_asins, eligible, item_users)
        pop_z = popularity_scores(eligible)
        tail_z = tail_adjustment_scores(eligible, head_threshold)
    else:
        text_z = attr_z = collab_z = pop_z = tail_z = []
    position = {i: k for k, i in enumerate(eligible_idx)}

    results: List[M2Score] = []
    for i, cand in enumerate(candidates):
        asin = cand["parent_asin"]
        if exclusion[i]:
            results.append(
                M2Score(
                    item_id=asin,
                    score=float("-inf"),
                    score_components={},
                    excluded=True,
                    exclusion_reason_codes=exclusion[i],
                )
            )
            continue

        k = position[i]
        components = {
            "eligibility": 1.0,  # survived the gate; see docstring
            "text_relevance": text_z[k],
            "attribute_match": attr_z[k],
            "collaborative": collab_z[k],
            "popularity": pop_z[k],
            "tail_adjustment": tail_z[k],
        }
        score = sum(weights.get(name, 0.0) * v_ for name, v_ in components.items() if name != "eligibility")

        reason_codes: List[str] = []
        if is_cold:
            reason_codes.append("POPULAR_FALLBACK")
        else:
            if text_z[k] > USE_CASE_MATCH_ZSCORE_THRESHOLD:
                reason_codes.append("USE_CASE_MATCH")
            if attr_z[k] > ATTRIBUTE_MATCH_THRESHOLD:
                reason_codes.append("ATTRIBUTE_MATCH")
            if collab_z[k] > HISTORY_AFFINITY_ZSCORE_THRESHOLD:
                reason_codes.append("HISTORY_AFFINITY")

        flags = ["PRICE_MISSING"] if _candidate_price(cand) is None else []
        results.append(
            M2Score(item_id=asin, score=score, score_components=components, reason_codes=reason_codes, flags=flags)
        )

    return results


# ---------------------------------------------------------------------------
# RankerResult and the fallback contract
# ---------------------------------------------------------------------------


@dataclass
class RankedItem:
    item_id: str
    rank: int
    score: float
    score_components: Dict[str, float]
    reason_codes: List[str]


@dataclass
class ExcludedItem:
    item_id: str
    reason_codes: List[str]


@dataclass
class RankerResult:
    """The ranker's answer, shaped like the proposal's RankerResultV1 (ranked,
    excluded, warnings, ranker-only latency)."""

    ranked: List[RankedItem]
    excluded: List[ExcludedItem]
    warnings: List[str]
    latency_ms: float
    fell_back: bool = False


def _fallback(candidates: List[dict], code: str, t0: float) -> RankerResult:
    """The candidates unchanged, in their original order, with the reason."""
    ranked = [
        RankedItem(item_id=c["parent_asin"], rank=i + 1, score=0.0, score_components={}, reason_codes=[])
        for i, c in enumerate(candidates)
    ]
    return RankerResult(
        ranked=ranked,
        excluded=[],
        warnings=[code],
        latency_ms=(time.perf_counter() - t0) * 1000.0,
        fell_back=True,
    )


def _validate(scored: Any, input_ids: List[str]) -> Optional[str]:
    """None when the scorer's output is usable, else the fallback warning code."""
    if not scored and input_ids:
        return "FALLBACK_EMPTY_OUTPUT"
    ids = [s.item_id for s in scored]
    if len(set(ids)) != len(ids):
        return "FALLBACK_DUPLICATE_ID"
    known = set(input_ids)
    if any(i not in known for i in ids):
        return "FALLBACK_UNKNOWN_ID"
    if set(ids) != known:
        return "FALLBACK_INCOMPLETE_OUTPUT"
    return None


def rank_candidates(
    *,
    candidates: List[dict],
    history_asins: List[str],
    history_categories: Set[str],
    history_brands: Set[str],
    items: Dict[str, dict],
    item_users: Dict[str, Set[str]],
    head_threshold: int,
    constraints: Optional[ProductConstraints] = None,
    weights: Optional[Dict[str, float]] = None,
    timeout_s: Optional[float] = None,
    scorer: Callable[..., List[M2Score]] = score_candidates,
) -> RankerResult:
    """Rank `candidates`, or fall back to their original order.

    Falls back (with a ``FALLBACK_*`` warning) on an error, on a timeout, on an
    empty output, and on a duplicate, unknown or missing id in the output. Every
    input id ends up exactly once in ``ranked`` or ``excluded``. Candidates the
    gate removed are in ``excluded`` with their reason codes; if the gate removes
    all of them, ``ranked`` is legitimately empty and that is not a fallback.
    """
    t0 = time.perf_counter()
    input_ids = [c["parent_asin"] for c in candidates]
    if not candidates:
        return RankerResult([], [], [], 0.0)
    if len(set(input_ids)) != len(input_ids):
        return _fallback(candidates, "FALLBACK_DUPLICATE_INPUT_ID", t0)

    kwargs = dict(
        history_asins=history_asins,
        history_categories=history_categories,
        history_brands=history_brands,
        candidates=candidates,
        items=items,
        item_users=item_users,
        head_threshold=head_threshold,
        constraints=constraints,
        weights=weights,
    )
    try:
        if timeout_s is None:
            scored = scorer(**kwargs)
        else:
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(scorer, **kwargs)
            try:
                scored = future.result(timeout=timeout_s)
            except FutureTimeout:
                pool.shutdown(wait=False, cancel_futures=True)  # do not wait for the slow call
                return _fallback(candidates, "FALLBACK_TIMEOUT", t0)
            finally:
                pool.shutdown(wait=False)
    except Exception:  # any scorer failure must not break the request
        return _fallback(candidates, "FALLBACK_ERROR", t0)

    problem = _validate(scored, input_ids)
    if problem:
        return _fallback(candidates, problem, t0)

    kept = [i for i, s in enumerate(scored) if not s.excluded]
    order = sorted(kept, key=lambda i: scored[i].score, reverse=True)  # ties keep input order
    ranked = [
        RankedItem(
            item_id=scored[i].item_id,
            rank=r + 1,
            score=scored[i].score,
            score_components=scored[i].score_components,
            reason_codes=scored[i].reason_codes,
        )
        for r, i in enumerate(order)
    ]
    excluded = [
        ExcludedItem(item_id=s.item_id, reason_codes=s.exclusion_reason_codes) for s in scored if s.excluded
    ]
    warnings: List[str] = []
    if not history_asins:
        warnings.append("COLD_START")
    if any("PRICE_MISSING" in scored[i].flags for i in kept):
        warnings.append("PRICE_MISSING")
    return RankerResult(
        ranked=ranked,
        excluded=excluded,
        warnings=warnings,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )
