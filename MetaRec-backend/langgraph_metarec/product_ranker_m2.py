"""M2 — constraint-aware hybrid ranker for the product domain. WP4 D3.

M2's definition (WP4-proposal-source.md, line 48): "Hard eligibility first,
then query/use-case and attribute relevance + authenticated-user affinity
when available + calibrated popularity/head-tail control."

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

"Authenticated-user affinity" (line 48) is explicitly conditional on live
history existing — and Yanny's correction says not to rely on live MetaRec
interaction data, since ``item_interactions`` starts empty. For this offline
evaluation on the frozen Amazon Reviews 2023 dataset, the honest stand-in is
the same collaborative (ItemKNN) signal already built for D2: a user's
training-history co-purchase pattern is the only affinity signal this dataset
can actually provide. ``HISTORY_AFFINITY`` is this component's reason code.
"""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from rank_bm25 import BM25Okapi

from langgraph_metarec.product_constraints import ProductConstraints, violations

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Fixed weights on the six required components (sum to 1.0 for interpretability
# — z-scoring already puts them on a comparable scale, so this isn't required,
# just tidy). Starting point reuses B1's tuned text/popularity balance
# (0.25 text : 0.75 popularity) and carves out room for the two new signals;
# these are NOT yet tuned on dev data the way B1's weight was — that is the
# next step after this module is verified to compute correctly.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "text_relevance": 0.25,
    "attribute_match": 0.10,
    "collaborative": 0.15,
    "popularity": 0.45,
    "tail_adjustment": 0.05,
}

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


def violation_keys_to_reason_codes(violation_keys: Sequence[str]) -> List[str]:
    codes = [_VIOLATION_TO_REASON_CODE[k] for k in violation_keys if k in _VIOLATION_TO_REASON_CODE]
    return list(dict.fromkeys(codes))  # de-duplicate, preserve order (compat+exclude can both fire WRONG_MODEL)


def _enriched_text(item_meta: dict) -> str:
    """title + brand + main_category + store, not title alone. Verified on a
    30K-case sample (warm users only) this beats title-only BM25 by a wide
    margin: +7.1% Recall@10, +9.5% NDCG@10, +11.1% MRR@10. Titles are often
    marketing copy with inconsistent wording; brand/category/store are
    cleaner, more consistent fields that catch matches titles alone miss."""
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
    if not query_tokens:
        return [0.0] * len(candidates)
    corpus = [tokenize(_enriched_text(c)) for c in candidates]
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
    """Full M2 hybrid score for every candidate: hard eligibility gate first,
    then the five ranking components combined by `weights`.

    `constraints` defaults to empty (ProductConstraints() with every field
    unset), which is_empty() -> True, so the gate passes everything through —
    this offline dataset has no stated budget/brand/category per case (see
    module docstring), so eligibility is a structural no-op here. Its real
    test ground is the 15 labelled constraint fixtures from D0
    (tests/wp4/cases/), which DO carry real stated constraints.
    """
    weights = weights or DEFAULT_WEIGHTS
    c = constraints or ProductConstraints()
    is_cold = not history_asins

    text_z = text_relevance_scores(history_asins, candidates, items)
    attr_z = attribute_match_scores(history_categories, history_brands, candidates)
    collab_z = collaborative_scores(history_asins, candidates, item_users)
    pop_z = popularity_scores(candidates)
    tail_z = tail_adjustment_scores(candidates, head_threshold)

    results: List[M2Score] = []
    for i, cand in enumerate(candidates):
        asin = cand["parent_asin"]
        v = violations(cand, c)
        if v:
            results.append(
                M2Score(
                    item_id=asin,
                    score=float("-inf"),
                    score_components={},
                    excluded=True,
                    exclusion_reason_codes=violation_keys_to_reason_codes(v),
                )
            )
            continue

        components = {
            "eligibility": 1.0,  # survived the gate; see docstring on why this is a near-constant here
            "text_relevance": text_z[i],
            "attribute_match": attr_z[i],
            "collaborative": collab_z[i],
            "popularity": pop_z[i],
            "tail_adjustment": tail_z[i],
        }
        score = sum(weights.get(k, 0.0) * v_ for k, v_ in components.items() if k != "eligibility")

        reason_codes: List[str] = []
        if is_cold:
            reason_codes.append("POPULAR_FALLBACK")
        else:
            if text_z[i] > USE_CASE_MATCH_ZSCORE_THRESHOLD:
                reason_codes.append("USE_CASE_MATCH")
            if attr_z[i] > ATTRIBUTE_MATCH_THRESHOLD:
                reason_codes.append("ATTRIBUTE_MATCH")
            if collab_z[i] > HISTORY_AFFINITY_ZSCORE_THRESHOLD:
                reason_codes.append("HISTORY_AFFINITY")

        results.append(
            M2Score(item_id=asin, score=score, score_components=components, reason_codes=reason_codes)
        )

    return results
