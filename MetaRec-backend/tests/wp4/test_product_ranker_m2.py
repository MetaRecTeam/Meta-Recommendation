"""WP4 D3 — the M2 ranker: reason codes, the missing-price policy, eligibility first,
the result contract, and the fallback contract.

No network and no dataset files: candidates are small hand-written cards. The last
test runs the M2 gate over the 15 labelled constraint fixtures from D0.
"""

import time

import pytest

from langgraph_metarec import product_ranker_m2 as m2
from langgraph_metarec.graphs.generic_graph import normalize_tool_items
from langgraph_metarec.product_constraints import ProductConstraints
from tests.wp4.dataset import load_cases

pytestmark = pytest.mark.backend_unit

HEAD = 100  # head threshold for tail_adjustment


def card(asin, title, price=80.0, brand="KeyBrand", category="Computers", reviews=50, **extra):
    c = {
        "parent_asin": asin,
        "title": title,
        "price": price,
        "brand": brand,
        "main_category": category,
        "store": brand,
        "rating_number": reviews,
    }
    c.update(extra)
    return c


# The user's one past purchase, and who bought what (for the collaborative signal).
ITEMS = {"H1": {"title": "Quiet Mechanical Keyboard Pro", "brand": "KeyBrand", "main_category": "Computers", "store": "KeyStore"}}
ITEM_USERS = {"H1": {"u1", "u2", "u3"}, "C1": {"u1", "u2", "x"}}
HIST = dict(history_asins=["H1"], history_categories={"Computers"}, history_brands={"KeyBrand"})

C1 = card("C1", "Wireless Mechanical Keyboard", reviews=50)  # matches the history in every way
C2 = card("C2", "Garden Hose 50ft", brand="HoseCo", category="Garden", reviews=500)
C3 = card("C3", "Membrane Keyboard Basic", brand="OtherBrand", price=30.0, reviews=10)
POOL = [C1, C2, C3]


def rank(candidates, constraints=None, **overrides):
    kwargs = dict(
        candidates=candidates,
        items=ITEMS,
        item_users=ITEM_USERS,
        head_threshold=HEAD,
        constraints=constraints,
        **HIST,
    )
    kwargs.update(overrides)
    return m2.rank_candidates(**kwargs)


def codes_of(result):
    ranked = {r.item_id: r.reason_codes for r in result.ranked}
    excluded = {e.item_id: e.reason_codes for e in result.excluded}
    return ranked, excluded


# ---------------------------------------------------------------------------
# Reason codes: every allow-listed code is produced, and nothing else is
# ---------------------------------------------------------------------------


def test_positive_reason_codes():
    ranked, _ = codes_of(rank(POOL))
    assert "USE_CASE_MATCH" in ranked["C1"]  # title/brand/category/store overlap the history
    assert "ATTRIBUTE_MATCH" in ranked["C1"]  # same category and brand as the history
    assert "HISTORY_AFFINITY" in ranked["C1"]  # shares buyers with the history item
    assert ranked["C2"] == []  # matches nothing: no positive reason is invented


def test_cold_user_gets_popular_fallback_and_a_cold_start_warning():
    result = rank(POOL, history_asins=[], history_categories=set(), history_brands=set(),
                  weights={"popularity": 1.0})
    ranked, _ = codes_of(result)
    assert all(codes == ["POPULAR_FALLBACK"] for codes in ranked.values())
    assert "COLD_START" in result.warnings
    assert [r.item_id for r in result.ranked][0] == "C2"  # most reviewed first when only popularity counts


def test_over_budget():
    pool = POOL + [card("C4", "Pro Mechanical Keyboard", price=150.0)]
    _, excluded = codes_of(rank(pool, ProductConstraints(budget_max=100, currency="USD")))
    assert excluded == {"C4": ["OVER_BUDGET"]}


def test_wrong_brand_both_directions():
    pool = POOL + [card("C5", "Apple Keyboard", brand="Apple")]
    _, excluded = codes_of(rank(pool, ProductConstraints(brand_not_in=["Apple"])))
    assert excluded == {"C5": ["WRONG_BRAND"]}
    _, excluded = codes_of(rank(POOL, ProductConstraints(brand_in=["KeyBrand"])))
    assert excluded == {"C2": ["WRONG_BRAND"], "C3": ["WRONG_BRAND"]}  # a different brand is readable


def test_wrong_category_both_directions():
    _, excluded = codes_of(rank(POOL, ProductConstraints(category_exclude_terms=["membrane"])))
    assert excluded == {"C3": ["WRONG_CATEGORY"]}
    _, excluded = codes_of(rank(POOL, ProductConstraints(category_terms=["keyboard"])))
    assert excluded == {"C2": ["WRONG_CATEGORY"]}


def test_wrong_model_for_missing_requirement_and_for_excluded_term_without_duplicates():
    pool = [C1, card("C6", "Mechanical Keyboard Bluetooth Wireless"), card("C7", "Plain Keyboard Bluetooth")]
    _, excluded = codes_of(rank(pool, ProductConstraints(require_terms=["wireless"])))
    assert excluded == {"C7": ["WRONG_MODEL"]}  # lacks a required word
    _, excluded = codes_of(rank(pool, ProductConstraints(exclude_terms=["bluetooth"])))
    assert excluded == {"C6": ["WRONG_MODEL"], "C7": ["WRONG_MODEL"]}  # contains an excluded word
    _, excluded = codes_of(rank(pool, ProductConstraints(require_terms=["wireless"], exclude_terms=["bluetooth"])))
    assert excluded["C7"] == ["WRONG_MODEL"]  # fails both rules, one code


def test_unknown_price_is_excluded_when_a_budget_is_stated():
    pool = POOL + [card("C8", "Mystery Keyboard", price=None)]
    result = rank(pool, ProductConstraints(budget_max=100, currency="USD"))
    _, excluded = codes_of(result)
    assert excluded == {"C8": ["UNKNOWN_PRICE_STRICT_MODE"]}  # never treated as free, never "over budget"
    assert "PRICE_MISSING" not in result.warnings  # it is gone, so nothing to flag


def test_unknown_price_is_kept_and_flagged_when_no_budget_is_stated():
    pool = POOL + [card("C8", "Mystery Keyboard", price=None)]
    result = rank(pool, ProductConstraints(category_terms=["keyboard"]))
    ranked, excluded = codes_of(result)
    assert "C8" in ranked and "C8" not in excluded
    assert "PRICE_MISSING" in result.warnings


def test_different_currency_cannot_be_compared_with_the_budget():
    pool = POOL + [card("C9", "Cheap-looking Keyboard", price=500.0, currency="SGD"),
                   card("C10", "Cheaper Keyboard", price=10.0, currency="SGD")]
    _, excluded = codes_of(rank(pool, ProductConstraints(budget_max=100, currency="USD")))
    # 500 SGD is not "over budget" and 10 SGD is not "within budget": neither is comparable.
    assert excluded == {"C9": ["UNKNOWN_PRICE_STRICT_MODE"], "C10": ["UNKNOWN_PRICE_STRICT_MODE"]}


def test_every_allow_listed_code_is_reachable_and_nothing_outside_it_is_emitted():
    seen = set()
    for result in (
        rank(POOL),
        rank(POOL, history_asins=[], history_categories=set(), history_brands=set()),
        rank(POOL + [card("C4", "Pro Mechanical Keyboard", price=150.0)], ProductConstraints(budget_max=100)),
        rank(POOL, ProductConstraints(brand_in=["KeyBrand"])),
        rank(POOL, ProductConstraints(category_exclude_terms=["membrane"])),
        rank(POOL, ProductConstraints(require_terms=["wireless"])),
        rank(POOL + [card("C8", "Mystery Keyboard", price=None)], ProductConstraints(budget_max=100)),
    ):
        seen.update(c for r in result.ranked for c in r.reason_codes)
        seen.update(c for e in result.excluded for c in e.reason_codes)
    assert seen == set(m2.REASON_CODE_ALLOW_LIST)


# ---------------------------------------------------------------------------
# Eligibility first
# ---------------------------------------------------------------------------


def test_a_removed_candidate_cannot_change_how_the_survivors_are_scored():
    outlier = card("BIG", "Pro Mechanical Keyboard", price=900.0, reviews=10_000_000)
    with_outlier = rank(POOL + [outlier], ProductConstraints(budget_max=100, currency="USD"))
    survivors_only = rank(POOL)
    a = {r.item_id: (r.score, r.score_components) for r in with_outlier.ranked}
    b = {r.item_id: (r.score, r.score_components) for r in survivors_only.ranked}
    assert a == b  # same scores to the last digit, as if the outlier had never been there
    assert [r.item_id for r in with_outlier.ranked] == [r.item_id for r in survivors_only.ranked]


def test_without_constraints_the_gate_changes_nothing():
    """Guards the refactor: with nothing stated, scores equal the plain weighted sum of the five z-scores."""
    scored = m2.score_candidates(candidates=POOL, items=ITEMS, item_users=ITEM_USERS, head_threshold=HEAD, **HIST)
    parts = {
        "text_relevance": m2.text_relevance_scores(["H1"], POOL, ITEMS),
        "attribute_match": m2.attribute_match_scores({"Computers"}, {"KeyBrand"}, POOL),
        "collaborative": m2.collaborative_scores(["H1"], POOL, ITEM_USERS),
        "popularity": m2.popularity_scores(POOL),
        "tail_adjustment": m2.tail_adjustment_scores(POOL, HEAD),
    }
    for i, s in enumerate(scored):
        expected = sum(m2.DEFAULT_WEIGHTS[k] * v[i] for k, v in parts.items())
        assert s.score == pytest.approx(expected, abs=1e-12)
        assert not s.excluded


def test_excluded_candidates_score_minus_infinity_and_everyone_is_accounted_for():
    pool = POOL + [card("C4", "Pro Mechanical Keyboard", price=150.0)]
    scored = m2.score_candidates(candidates=pool, items=ITEMS, item_users=ITEM_USERS, head_threshold=HEAD,
                                 constraints=ProductConstraints(budget_max=100), **HIST)
    assert [s.item_id for s in scored] == [c["parent_asin"] for c in pool]  # input order kept
    assert scored[-1].excluded and scored[-1].score == float("-inf")

    result = rank(pool, ProductConstraints(budget_max=100))
    ids = [r.item_id for r in result.ranked] + [e.item_id for e in result.excluded]
    assert sorted(ids) == sorted(c["parent_asin"] for c in pool)  # each input id exactly once
    assert [r.rank for r in result.ranked] == list(range(1, len(result.ranked) + 1))
    assert result.latency_ms >= 0 and not result.fell_back


def test_ties_keep_the_input_order():
    twin_a = card("T1", "Same Keyboard")
    twin_b = card("T2", "Same Keyboard")
    result = rank([twin_a, twin_b], history_asins=[], history_categories=set(), history_brands=set())
    assert [r.item_id for r in result.ranked] == ["T1", "T2"]


def test_all_candidates_removed_is_a_valid_empty_ranking_not_a_fallback():
    pool = [card("A", "Pro Keyboard", price=500.0), card("B", "Max Keyboard", price=600.0)]
    result = rank(pool, ProductConstraints(budget_max=100))
    assert result.ranked == [] and not result.fell_back
    assert [e.item_id for e in result.excluded] == ["A", "B"]
    assert all(e.reason_codes == ["OVER_BUDGET"] for e in result.excluded)


def test_empty_input_gives_an_empty_result():
    result = rank([])
    assert result.ranked == [] and result.excluded == [] and not result.fell_back


# ---------------------------------------------------------------------------
# Fallback: error, timeout, empty output, duplicate / unknown / missing id
# ---------------------------------------------------------------------------


def _original_order_with(result, code):
    assert result.fell_back and result.warnings == [code]
    assert [r.item_id for r in result.ranked] == [c["parent_asin"] for c in POOL]  # the original order
    assert [r.rank for r in result.ranked] == [1, 2, 3]
    assert result.excluded == []


def _scores(ids):
    return [m2.M2Score(item_id=i, score=float(k), score_components={}) for k, i in enumerate(ids)]


def test_fallback_on_error():
    def boom(**_):
        raise RuntimeError("scorer failed")

    _original_order_with(rank(POOL, scorer=boom), "FALLBACK_ERROR")


def test_fallback_on_timeout_without_waiting_for_the_slow_call():
    def slow(**_):
        time.sleep(0.6)
        return _scores(["C1", "C2", "C3"])

    t0 = time.perf_counter()
    result = rank(POOL, scorer=slow, timeout_s=0.05)
    assert time.perf_counter() - t0 < 0.4  # returned long before the scorer finished
    _original_order_with(result, "FALLBACK_TIMEOUT")


def test_a_fast_scorer_inside_the_timeout_is_not_a_fallback():
    result = rank(POOL, timeout_s=5.0)
    assert not result.fell_back and len(result.ranked) == 3


def test_fallback_on_empty_output():
    _original_order_with(rank(POOL, scorer=lambda **_: []), "FALLBACK_EMPTY_OUTPUT")


def test_fallback_on_duplicate_id_in_the_output():
    _original_order_with(rank(POOL, scorer=lambda **_: _scores(["C1", "C1", "C3"])), "FALLBACK_DUPLICATE_ID")


def test_fallback_on_unknown_id_in_the_output():
    _original_order_with(rank(POOL, scorer=lambda **_: _scores(["C1", "C2", "ZZZ"])), "FALLBACK_UNKNOWN_ID")


def test_fallback_on_missing_id_in_the_output():
    _original_order_with(rank(POOL, scorer=lambda **_: _scores(["C1", "C2"])), "FALLBACK_INCOMPLETE_OUTPUT")


def test_fallback_on_duplicate_input_id():
    pool = [C1, C1, C3]
    result = rank(pool)
    assert result.fell_back and result.warnings == ["FALLBACK_DUPLICATE_INPUT_ID"]
    assert [r.item_id for r in result.ranked] == ["C1", "C1", "C3"]


# ---------------------------------------------------------------------------
# The M2 gate against the 15 labelled constraint fixtures (the D0 corpus)
# ---------------------------------------------------------------------------


def test_m2_gate_on_the_labelled_fixtures():
    """Every labelled violator is removed; the only compliant products removed are those whose price is
    unknown while a budget is stated, which is exactly the declared strict policy."""
    violators = removed_violators = compliant = kept_compliant = strict_policy_removed = 0
    for case in load_cases():
        constraints = ProductConstraints.from_dict(case.hard_constraints) if isinstance(
            case.hard_constraints, dict) else case.hard_constraints
        labels = case.labels_by_title()
        for item in normalize_tool_items("amazon.product.search", case.pool, "product"):
            codes = m2.exclusion_reason_codes(item, constraints)
            if labels.get(item["title"]):
                violators += 1
                removed_violators += bool(codes)
            else:
                compliant += 1
                if not codes:
                    kept_compliant += 1
                else:
                    assert codes == ["UNKNOWN_PRICE_STRICT_MODE"], (case.id, item["title"][:50], codes)
                    assert m2._candidate_price(item) is None
                    strict_policy_removed += 1
    assert violators == 52 and removed_violators == 52  # all 52 labelled violators removed
    assert compliant == 94 and kept_compliant + strict_policy_removed == 94
