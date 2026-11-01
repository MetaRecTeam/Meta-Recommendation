"""D5: the one-command evaluator (eval/domain_ranker.py).

Network-free and independent of the 1.1 GB fixture: the scoring path is run on a small synthetic catalogue,
and the statistics on arrays built by hand, so every expected number can be checked without trusting the
code under test.
"""
from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

import numpy as np
import pytest

from eval import domain_ranker as D
from langgraph_metarec.product_ranker_live import LIVE_WEIGHTS

pytestmark = pytest.mark.backend_unit

# ---------------------------------------------------------------------------
# The sealed test split
# ---------------------------------------------------------------------------


def test_the_test_split_is_refused_without_the_explicit_flag():
    with tempfile.TemporaryDirectory() as tmp:  # not tmp_path: pytest.ini's basetemp needs its parent folder to exist
        base = Path(tmp)
        fixture = base / "product_test_v1.jsonl"
        fixture.write_text("", encoding="utf-8")
        with pytest.raises(SystemExit) as err:
            D.main(["--domain", "product", "--fixture", str(fixture), "--systems", "generic", "--out", str(base / "o")])
        assert "sealed" in str(err.value)
        assert not (base / "o").exists(), "nothing is created before the split is checked"


def test_the_flag_lets_the_test_split_through_and_the_dev_split_never_needs_it():
    assert D.check_split_allowed(Path("eval/product_test_v1.jsonl"), True) == "test"
    assert D.check_split_allowed(Path("eval/product_dev_v1.jsonl"), False) == "dev"
    with pytest.raises(SystemExit):
        D.check_split_allowed(Path("eval/product_test_v1.jsonl"), False)


@pytest.mark.parametrize("name", ["product_test_v1.jsonl", "test_cases.jsonl", "cases_test.jsonl", "PRODUCT_TEST_V2.JSONL"])
def test_every_way_of_naming_the_test_fixture_is_caught(name):
    assert D.split_of(Path("eval") / name) == "test"


def test_held_out_users_are_the_nine_in_ten_not_used_for_tuning_and_the_test_split_scores_everyone():
    assert not D.is_held_out(0, "dev") and not D.is_held_out(40, "dev")
    assert all(D.is_held_out(u, "dev") for u in (1, 9, 11, 99))
    assert all(D.is_held_out(u, "test") for u in (0, 10, 7))


def test_the_test_split_counts_dev_as_past_but_the_dev_split_does_not():
    assert D.count_splits("dev") == ["train"]
    assert D.count_splits("test") == ["train", "dev"]


# ---------------------------------------------------------------------------
# System groups and the weights
# ---------------------------------------------------------------------------


def test_the_groups_expand_to_the_systems_in_a_fixed_order():
    assert D.systems_for(["domain_v1", "generic"]) == ["b0_legacy", "b0_reviews", "m2"]
    assert D.systems_for(["classical"]) == ["b1", "itemknn"]
    assert D.systems_for(["generic", "classical", "domain_v1", "generic"]) == ["b0_legacy", "b0_reviews", "b1", "itemknn", "m2"]


def test_m2_is_scored_at_the_weights_the_live_ranker_uses():
    assert LIVE_WEIGHTS == {"text_relevance": 0.25, "attribute_match": 0.0, "collaborative": 0.05, "popularity": 0.65, "tail_adjustment": 0.05}
    assert math.isclose(sum(LIVE_WEIGHTS.values()), 1.0)


# ---------------------------------------------------------------------------
# Metrics, slices and the bootstrap
# ---------------------------------------------------------------------------


def test_metrics_for_one_relevant_item():
    recall, ndcg, mrr = D.metric_arrays(np.array([1, 2, 3, 10, 11, 100]))
    assert recall.tolist() == [1, 1, 1, 1, 0, 0]
    assert ndcg[0] == 1.0 and math.isclose(ndcg[2], 1 / math.log2(4)) and math.isclose(ndcg[3], 1 / math.log2(11))
    assert ndcg[4] == ndcg[5] == 0.0
    assert math.isclose(mrr[1], 0.5) and math.isclose(mrr[3], 0.1) and mrr[4] == 0.0


def _data(history_words, target_counts, cold=None, head_threshold=12, priced=None):
    n = len(history_words)
    return {
        "hist_words": np.array(history_words),
        "tcount": np.array(target_counts),
        "cold": np.array(cold if cold is not None else [w == 0 for w in history_words]),
        "tpriced": np.array(priced if priced is not None else [True] * n),
        "head_threshold": head_threshold,
    }


def test_slices_cut_at_51_words_and_at_the_head_threshold():
    data = _data([0, 1, 51, 52, 400, 30], [0, 11, 12, 13, 5, 100])
    masks = D.slice_masks(data, np.ones(6, dtype=bool))
    assert masks["cold"].tolist() == [True, False, False, False, False, False]
    assert masks["warm"].tolist() == [False, True, True, True, True, True]
    assert masks["keyword"].tolist() == [False, True, True, False, False, True], "51 words is still keyword-like"
    assert masks["complex"].tolist() == [False, False, False, True, True, False], "52 words is the first complex case"
    assert masks["head"].tolist() == [False, False, True, True, False, True], "12 reviews is head"
    assert (masks["head"] ^ masks["tail"]).all()


def test_the_missing_price_slice_is_the_cases_whose_real_item_has_no_parsed_price():
    data = _data([60, 60, 0], [20, 20, 20], priced=[True, False, False])
    masks = D.slice_masks(data, np.ones(3, dtype=bool))
    assert masks["no_price"].tolist() == [False, True, True]
    assert "no_price" in dict(D.SLICES)


def test_slices_only_ever_contain_held_out_cases():
    data = _data([60, 60, 0], [20, 20, 20])
    masks = D.slice_masks(data, np.array([True, False, True]))
    assert all(not m[1] for m in masks.values())


def test_the_bootstrap_is_the_user_level_resample_and_repeatable():
    uinv = np.array([0, 0, 1, 2, 2, 2])
    columns = np.array([[1, 0], [0, 0], [1, 1], [0, 1], [0, 1], [1, 0]], dtype=float)
    a = D.bootstrap_sums(uinv, 3, columns, 50, 42)
    b = D.bootstrap_sums(uinv, 3, columns, 50, 42)
    assert np.array_equal(a, b)
    per_user = np.array([[1, 0], [1, 1], [1, 2]], dtype=float)  # what each user contributes
    rng = np.random.default_rng(42)
    expected = np.array([per_user[rng.integers(0, 3, 3)].sum(axis=0) for _ in range(50)])
    assert np.allclose(a, expected), "each resample draws whole users, with replacement"
    assert not np.array_equal(a, D.bootstrap_sums(uinv, 3, columns, 50, 43))


def test_an_empty_slice_has_no_interval_instead_of_crashing():
    point, lo, hi = D.lift_interval(np.zeros(5), np.zeros(5), 0.0, 0.0)
    assert math.isnan(point) and math.isnan(lo) and math.isnan(hi)


def test_the_lift_interval_brackets_a_known_ratio():
    num, den = np.full(100, 1.10), np.full(100, 1.0)
    point, lo, hi = D.lift_interval(num, den, 1.1, 1.0)
    assert math.isclose(point, 0.10) and math.isclose(lo, 0.10) and math.isclose(hi, 0.10)


# ---------------------------------------------------------------------------
# The decision rule, on arrays built by hand
# ---------------------------------------------------------------------------


def _analysis_data(m2_ranks, b1_ranks, words, users, *others):
    """Rank columns in system order: the extra columns first, then b1, then m2."""
    n = len(users)
    columns = [*others, b1_ranks, m2_ranks]
    S = len(columns)
    ranks = np.stack([np.asarray(c) for c in columns], axis=1).astype(np.int16)
    return {
        "ranks": ranks,
        "top": np.zeros((n, S, 10), dtype=np.int8),
        "head10": np.zeros((n, S), dtype=np.int8),
        "pop10": np.zeros((n, S), dtype=np.float32),
        "uid": np.asarray(users, dtype=np.int64),
        "hist_words": np.asarray(words),
        "cold": np.asarray(words) == 0,
        "tcount": np.full(n, 20),
        "tpriced": np.ones(n, dtype=bool),
        "retrieved": np.ones(n, dtype=bool),
        "valid10": np.ones((n, S), dtype=bool),
        "constrained": np.zeros(n, dtype=bool),
        "n_pool": np.full(n, 100, dtype=np.int16),
        "n_priced": np.full(n, 70, dtype=np.int16),
        "n_mapped": np.full(n, 100, dtype=np.int16),
        "shown": [np.array([0, 1, 2])] * (S - 1) + [np.array([0, 1, 2, 3])],
        "head_threshold": 12,
        "catalogue_size": 10,
    }


def _users(n_users, per_user):
    return [u for u in range(1, n_users + 1) for _ in range(per_user)]  # uid % 10 != 0 needs care below


def test_a_clear_win_meets_the_rule_and_a_tie_does_not():
    users = [u for u in range(1, 200) if u % 10 != 0][:120]
    users = [u for u in users for _ in range(4)]
    n = len(users)
    words = [60] * n
    b1 = [5] * n
    win = D.analyse(_analysis_data([2] * n, b1, words, users), ["b1", "m2"], "dev", None, 200)
    assert win["rule"]["met"] and all(win["rule"]["checks"].values())
    tie = D.analyse(_analysis_data(b1, b1, words, users), ["b1", "m2"], "dev", None, 200)
    assert not tie["rule"]["met"]
    assert not tie["rule"]["checks"]["lower_bound_above_zero"] and not tie["rule"]["checks"]["point_estimate_at_least_target"]
    assert tie["rule"]["checks"]["complex_slice_non_inferior"], "an equal ranker is not worse on the complex slice"


def test_a_ranker_that_wins_overall_but_loses_on_complex_cases_fails_the_non_inferiority_check():
    users = [u for u in range(1, 400) if u % 10 != 0][:200]
    kw_users, cx_users = users[:100], users[100:]
    allu = [u for u in kw_users for _ in range(4)] + [u for u in cx_users for _ in range(4)]
    words = [10] * 400 + [60] * 400
    b1 = [5] * 800
    m2 = [1] * 400 + [8] * 400  # better on keyword-like cases, much worse on complex ones
    out = D.analyse(_analysis_data(m2, b1, words, allu), ["b1", "m2"], "dev", None, 200)
    assert out["rule"]["complex"]["hi"] < D.NON_INFERIORITY
    assert not out["rule"]["checks"]["complex_slice_non_inferior"] and not out["rule"]["met"]


def test_tuning_users_are_never_scored_on_the_dev_split():
    users = [10, 10, 20, 20, 1, 1, 2, 2]  # 10 and 20 are tuning users
    out = D.analyse(_analysis_data([1] * 8, [5] * 8, [60] * 8, users), ["b1", "m2"], "dev", None, 50)
    assert out["n_cases_total"] == 8 and out["n_cases_held_out"] == 4 and out["n_users_held_out"] == 2


def _held_users(n_users, per_user=2):
    return [u for u in [x for x in range(1, 10 * n_users) if x % 10][:n_users] for _ in range(per_user)]


def test_a_target_that_was_never_retrieved_leaves_the_conditional_metrics_and_is_counted_separately():
    users = _held_users(40)
    n = len(users)
    data = _analysis_data([2] * n, [5] * n, [60] * n, users)
    data["retrieved"][:10] = False
    out = D.analyse(data, ["b1", "m2"], "dev", None, 20)
    assert out["table"]["all"]["_n_cases"]["v"] == n - 10 and out["n_retrieved_held_out"] == n - 10
    assert out["coverage"]["cases_target_not_retrieved"] == 10
    assert math.isclose(out["coverage"]["provider_candidate_recall"], (n - 10) / n)


def test_the_rule_compares_with_the_stronger_of_b0_and_b1_not_with_b1_alone():
    users = _held_users(120, 4)
    n = len(users)
    words = [60] * n
    strong_b0 = [1] * n   # B0 reviews-only ranks the real item first
    b1 = [5] * n
    m2 = [2] * n          # M2 beats B1 by a lot, but not the stronger B0
    out = D.analyse(_analysis_data(m2, b1, words, users, strong_b0), ["b0_reviews", "b1", "m2"], "dev", None, 100)
    assert out["rule"]["baseline"] == "b0_reviews" and not out["rule"]["met"]
    assert out["rule"]["overall"]["point"] < 0
    assert out["comparisons"]["m2_over_b1"]["all"]["ndcg"]["point"] > 0.03, "B1 alone would have been beaten"


def test_the_five_percent_lift_is_reported_as_directional_and_is_not_the_pass_mark():
    users = _held_users(150, 4)
    n = len(users)
    win = D.analyse(_analysis_data([2] * n, [3] * n, [60] * n, users), ["b1", "m2"], "dev", None, 100)  # rank 3 -> 2
    assert win["rule"]["directional_5pct_reached"] and win["rule"]["met"]
    tie = D.analyse(_analysis_data([11] * n, [11] * n, [60] * n, users), ["b1", "m2"], "dev", None, 100)
    assert not tie["rule"]["directional_5pct_reached"] and not tie["rule"]["met"]


def test_valid_fallback_shares_are_reported_for_cold_and_sparse_histories():
    users = _held_users(30)
    n = len(users)
    words = [0] * 20 + [5] * 20 + [60] * (n - 40)
    data = _analysis_data([2] * n, [5] * n, words, users)
    data["valid10"][:5, 1] = False          # M2 failed to return ten distinct candidates for five cold cases
    cov = D.analyse(data, ["b1", "m2"], "dev", None, 20)["coverage"]
    assert cov["valid_top10_share"]["b1"] == 1.0 and cov["valid_top10_share"]["m2"] < 1.0
    assert cov["valid_top10_share_cold"]["m2"] == 0.75 and cov["valid_top10_share_sparse"]["m2"] == 1.0


def _acceptance(an, constraint, latency, acceptance):
    return {r[0]: r for r in D.acceptance_rows(an, ["b1", "m2"], constraint, latency, acceptance)}


def test_the_acceptance_table_marks_each_bullet_from_the_numbers_and_fails_loudly():
    users = _held_users(120, 4)
    n = len(users)
    an = D.analyse(_analysis_data([2] * n, [5] * n, [60] * n, users), ["b1", "m2"], "dev", None, 100)
    good_constraint = {"total": {"domain_v1_violators_top10": 0, "pool_violators": 52, "generic_violators_top10": 52,
                                 "domain_v1_unpriced_shown": 0, "unpriced_in_pools": 3, "domain_v1_policy_excluded": 3}}
    latency = {"systems": {"m2": {"p95_ms": 15.0}}, "live_ranker_100": {"p95_ms": 4.0}}
    ok_tests = {"ran": True, "passed": True, "summary": "300 passed", "files": ["tests/wp4/test_b0_golden.py"]}
    rows = D.acceptance_rows(an, ["b1", "m2"], good_constraint, latency, ok_tests)
    assert [r[2] for r in rows if "violations" in r[0] or "p95" in r[0] or "Feature-off" in r[0]] == ["PASS", "PASS", "PASS"]
    # each failure shows up as FAIL, not as a quiet pass
    bad_constraint = {"total": {**good_constraint["total"], "domain_v1_violators_top10": 1}}
    assert any(r[2] == "FAIL" and "violations" in r[0] for r in D.acceptance_rows(an, ["b1", "m2"], bad_constraint, latency, ok_tests))
    slow = {"systems": {"m2": {"p95_ms": 26.0}}, "live_ranker_100": {"p95_ms": 4.0}}
    assert any(r[2] == "FAIL" and "p95" in r[0] for r in D.acceptance_rows(an, ["b1", "m2"], good_constraint, slow, ok_tests))
    failed = {**ok_tests, "passed": False, "summary": "2 failed"}
    assert any(r[2] == "FAIL" and "Feature-off" in r[0] for r in D.acceptance_rows(an, ["b1", "m2"], good_constraint, latency, failed))
    assert any(r[2] == "NOT RUN" for r in D.acceptance_rows(an, ["b1", "m2"], None, None, None))


def test_a_slow_live_ranker_fails_the_latency_bullet_even_when_the_offline_sample_is_fast():
    users = _held_users(120, 4)
    n = len(users)
    an = D.analyse(_analysis_data([2] * n, [5] * n, [60] * n, users), ["b1", "m2"], "dev", None, 100)
    slow_live = {"systems": {"m2": {"p95_ms": 5.0}}, "live_ranker_100": {"p95_ms": 26.0}}
    row = next(r for r in D.acceptance_rows(an, ["b1", "m2"], None, slow_live, None) if "p95" in r[0])
    assert row[2] == "FAIL"


def test_an_invalid_fallback_fails_the_cold_and_sparse_bullet():
    users = _held_users(30)
    n = len(users)
    data = _analysis_data([2] * n, [5] * n, [0] * n, users)
    data["valid10"][0, 1] = False
    an = D.analyse(data, ["b1", "m2"], "dev", None, 20)
    row = next(r for r in D.acceptance_rows(an, ["b1", "m2"], None, None, None) if "fallback" in r[0])
    assert row[2] == "FAIL"
    good = D.analyse(_analysis_data([2] * n, [5] * n, [0] * n, users), ["b1", "m2"], "dev", None, 20)
    assert next(r for r in D.acceptance_rows(good, ["b1", "m2"], None, None, None) if "fallback" in r[0])[2] == "PASS"


def test_m2_is_called_with_the_live_weights(monkeypatch):
    seen = {}
    real = D.M.score_candidates

    def spy(**kwargs):
        seen["weights"] = kwargs["weights"]
        return real(**kwargs)

    monkeypatch.setattr(D.M, "score_candidates", spy)
    res = _catalogue()
    case = json.loads(_case("c0", "0000000000000001", ASINS[97], [ASINS[3]]))
    cards = [D.R.item_card(a, res["items"]) for a in case["candidates"]]
    D.score_systems(case, cards, ["m2"], res)
    assert seen["weights"] == LIVE_WEIGHTS


def test_constraint_kinds_are_read_from_the_stated_constraint():
    from langgraph_metarec.product_constraints import ProductConstraints

    assert D.constraint_types(ProductConstraints(budget_max=50.0)) == ["budget"]
    assert D.constraint_types(ProductConstraints(budget_max=50.0, brand_not_in=["Apple"], require_terms=["wireless"])) == [
        "budget", "brand", "model / category terms"]
    assert D.constraint_types(ProductConstraints()) == []


def test_the_frozen_settings_are_the_ones_the_plan_fixed():
    assert (D.B1_BM25_WEIGHT, D.CUT_WORDS, D.HEAD_PERCENTILE, D.TUNING_MOD) == (0.25, 51, 0.80, 10)
    assert (D.BOOTSTRAP_DRAWS, D.BOOTSTRAP_SEED, D.TARGET_LIFT, D.NON_INFERIORITY) == (2000, 42, 0.03, -0.02)
    assert D.TOP_K == 10 and D.RULE_BASELINES == ("b0_legacy", "b0_reviews", "b1")
    assert D.DIRECTIONAL_LIFT == 0.05 and D.P95_TARGET_MS == 25.0


def test_exposure_and_coverage_are_read_from_the_held_out_top_tens():
    users = [1, 1, 2, 2]
    data = _analysis_data([1] * 4, [5] * 4, [60] * 4, users)
    data["head10"][:] = 10
    data["pop10"][:] = 3.0
    out = D.analyse(data, ["b1", "m2"], "dev", None, 20)
    assert out["exposure"]["m2"] == {"head_share_top10": 1.0, "mean_log_popularity_top10": 3.0,
                                      "distinct_items_shown": 4, "catalogue_coverage": 0.4}
    assert out["exposure"]["b1"]["distinct_items_shown"] == 3
    assert math.isclose(out["coverage"]["price_parsed_share"], 0.7) and out["coverage"]["catalogue_mapped_share"] == 1.0


def _write_graded(folder, *rows):
    (Path(folder) / "dev_graded_relevance.tsv").write_text("".join("\t".join(r) + "\n" for r in rows), encoding="utf-8")


def test_graded_labels_are_checked_against_the_cases_row_by_row(monkeypatch):
    data = {"case_id": ["a", "b"], "target": ["B1", "B2"], "cutoff": np.array([111, 222])}
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(D, "SPLIT_DIR", Path(tmp))
        _write_graded(tmp, ("u1", "B1", "111", "5.0", "2"), ("u2", "B2", "222", "4.0", "1"), ("u3", "B9", "999", "1.0", "0"))
        assert D.load_graded("dev", data).tolist() == [2, 1], "rows past the last case are ignored"
        _write_graded(tmp, ("u1", "B1", "111", "5.0", "2"), ("u2", "B2", "223", "4.0", "1"))
        with pytest.raises(SystemExit):
            D.load_graded("dev", data)
        _write_graded(tmp, ("u1", "B1", "111", "5.0", "2"), ("u2", "B7", "222", "4.0", "1"))
        with pytest.raises(SystemExit):
            D.load_graded("dev", data)
        assert D.load_graded("test", data) is None, "no graded file, no graded metric"


# ---------------------------------------------------------------------------
# The scoring pass on a synthetic catalogue
# ---------------------------------------------------------------------------

ASINS = [f"B{n:09d}" for n in range(100)]


def _catalogue():
    items = {}
    for n, asin in enumerate(ASINS):
        items[asin] = {
            "parent_asin": asin, "title": f"Widget {'alpha' if n % 3 == 0 else 'beta'} number {n}",
            "price": f"${10 + n}" if n % 4 else None, "average_rating": 4.0 + (n % 10) / 10, "rating_number": 0,
            "brand": f"Brand{n % 5}", "main_category": "Tools" if n % 2 else "Home", "store": f"Store{n % 7}",
        }
    # train counts: item n has n // 5 train users, so the 80th percentile is well above 0
    item_users = {asin: {f"u{u}" for u in range(n // 5)} for n, asin in enumerate(ASINS)}
    for asin, meta in items.items():
        meta["rating_number"] = len(item_users[asin])
    counts = sorted(m["rating_number"] for m in items.values())
    return {"items": items, "iu": item_users, "head": counts[int(len(counts) * D.HEAD_PERCENTILE)],
            "index": {a: i for i, a in enumerate(items)}}


def _case(case_id, user_key, target, history):
    hist_titles = " ".join(f"Widget alpha number {ASINS.index(h)}" for h in history)
    return json.dumps({
        "case_id": case_id, "user_key": user_key, "cutoff_time": 1000 + len(case_id), "query": hist_titles,
        "fused_preferences": {}, "history": [{"parent_asin": h, "timestamp": 1} for h in history],
        "candidates": ASINS, "ground_truth_item_ids": [target], "strict_constraints": {},
        "slice_labels": {"cold_warm": "warm" if history else "cold"},
    })


@pytest.fixture()
def synthetic_pass():
    res = _catalogue()
    systems = D.systems_for(["generic", "classical", "domain_v1"])
    D.G.clear()
    D.G.update(res, split="dev", systems=systems)
    lines = [_case("c0", "0000000000000001", ASINS[97], [ASINS[3], ASINS[9]]), _case("c1", "0000000000000002", ASINS[4], [])]
    yield D._work((0, lines)), res, systems
    D.G.clear()


def test_the_pass_records_a_rank_a_top_ten_and_the_slice_labels_for_every_system(synthetic_pass):
    out, res, systems = synthetic_pass
    assert out["ranks"].shape == (2, 5) and out["top"].shape == (2, 5, 10)
    assert out["case_id"] == ["c0", "c1"] and out["cold"].tolist() == [False, True]
    assert out["uid"].tolist() == [1, 2]
    assert out["tpos"].tolist() == [97, 4] and out["tcount"].tolist() == [97 // 5, 4 // 5]
    assert out["n_mapped"].tolist() == [100, 100] and (out["n_priced"] == 75).all()
    for si in range(5):
        for k in range(2):
            in_top = int(out["tpos"][k]) in set(out["top"][k, si].tolist())
            assert in_top == (out["ranks"][k, si] <= 10), "rank <= 10 exactly when the real item is in the top 10"
            assert len(set(out["top"][k, si].tolist())) == 10


def test_b0_reviews_only_ranks_by_review_count_then_title(synthetic_pass):
    out, res, systems = synthetic_pass
    si = systems.index("b0_reviews")
    items = res["items"]
    order = sorted(ASINS, key=lambda a: (items[a]["rating_number"], items[a]["title"]), reverse=True)
    assert out["ranks"][0, si] == order.index(ASINS[97]) + 1
    assert out["ranks"][1, si] == order.index(ASINS[4]) + 1
    assert out["head10"][0, si] == 10, "the ten most-reviewed items are all head items here"


def test_the_legacy_b0_uses_the_train_mean_rating_first(synthetic_pass):
    out, res, systems = synthetic_pass
    si = systems.index("b0_legacy")
    items = res["items"]
    order = sorted(ASINS, key=lambda a: (items[a]["average_rating"] or 0, items[a]["rating_number"], items[a]["title"]), reverse=True)
    assert out["ranks"][0, si] == order.index(ASINS[97]) + 1


def test_a_cold_user_gets_the_same_ranking_from_b1_itemknn_and_popularity(synthetic_pass):
    out, _, systems = synthetic_pass
    b1, knn = systems.index("b1"), systems.index("itemknn")
    assert out["ranks"][1, b1] == out["ranks"][1, knn]
    assert out["top"][1, b1].tolist() == out["top"][1, knn].tolist()


def test_the_pass_records_retrieval_price_knowledge_and_valid_top_tens(synthetic_pass):
    out, res, systems = synthetic_pass
    assert out["retrieved"].tolist() == [True, True] and out["valid10"].all() and not out["constrained"].any()
    assert out["n_pool"].tolist() == [100, 100]
    items = res["items"]
    # item 97 has a price (97 % 4 != 0); item 4 does not (4 % 4 == 0)
    assert out["tpriced"].tolist() == [items[ASINS[97]]["price"] is not None, items[ASINS[4]]["price"] is not None] == [True, False]


def test_a_case_whose_real_item_was_not_retrieved_is_recorded_as_a_miss_not_a_crash():
    res = _catalogue()
    systems = D.systems_for(["generic", "classical", "domain_v1"])
    D.G.clear()
    D.G.update(res, split="dev", systems=systems)
    try:
        line = json.loads(_case("c9", "0000000000000003", ASINS[5], [ASINS[1]]))
        line["candidates"] = [a for a in ASINS if a != ASINS[5]]  # the real item is not in the pool
        line["strict_constraints"] = {"budget_max": 50}
        out = D._work((0, [json.dumps(line)]))
    finally:
        D.G.clear()
    assert out["retrieved"].tolist() == [False] and out["tpos"].tolist() == [-1] and out["constrained"].tolist() == [True]
    assert (out["ranks"] == 100).all(), "a miss ranks one past the pool"
    assert out["valid10"].all() and out["n_pool"].tolist() == [99]


def test_coverage_only_counts_held_out_users(synthetic_pass):
    out, res, systems = synthetic_pass
    # user 1 and 2 are both held out on the dev split (uid % 10 != 0), so both contribute
    si = systems.index("b0_reviews")
    expected = {res["index"][ASINS[i]] for k in range(2) for i in out["top"][k, si]}
    assert set(out["shown"][si].tolist()) == expected
    D.G["split"] = "dev"
    tuning = _case("c2", "000000000000000a", ASINS[0], [])  # uid 10: a tuning user, never counted
    only_tuning = D._work((0, [tuning]))
    assert all(len(s) == 0 for s in only_tuning["shown"])


# ---------------------------------------------------------------------------
# Output and the live smoke test
# ---------------------------------------------------------------------------


def test_the_live_smoke_test_is_recorded_as_not_run_without_a_key_or_without_the_flag(monkeypatch):
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    assert D.live_smoke(False)["ran"] is False
    off = D.live_smoke(True)
    assert off["ran"] is False and "SERPAPI_KEY" in off["reason"]


def test_the_live_smoke_path_runs_end_to_end_against_a_stub_provider(monkeypatch):
    """The live smoke test is optional and needs a key, so its code would otherwise never run before the demo."""
    from langgraph_metarec import tool_registry as TR
    from langgraph_metarec.tool_registry import ToolRegistry, ToolSpec
    from tests.wp4.dataset import load_cases

    pools = {c.query: c.pool for c in load_cases()}

    def pool_for(params):
        # the graph appends terms to the search text ("... Corsair < 120 USD"), so match on the start of it
        text = (params or {}).get("query") or ""
        return next(pool for query, pool in pools.items() if text.startswith(query))

    def stub_registry():
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="amazon.product.search", domain="product", tags={"#thing", "#product", "#shopping"},
            input_schema={"type": "object"}, output_schema={"type": "array"},
            adapter=lambda params: list(pool_for(params)),
        ))
        return registry

    monkeypatch.setenv("SERPAPI_KEY", "not-a-real-key")  # present, but the stub registry never uses the network
    monkeypatch.setattr(TR, "build_default_tool_registry", stub_registry)
    out = D.live_smoke(True)
    assert out["ran"] is True and len(out["rows"]) == 2 * D.SMOKE_QUERIES
    assert {r["ranker"] for r in out["rows"]} == {"legacy", "domain_v1"}
    assert all(0 < r["returned"] <= 10 for r in out["rows"])
    assert all(r["violators_by_checker"] == 0 and not r["fell_back"] for r in out["rows"] if r["ranker"] == "domain_v1")
    legacy_violators = sum(r["violators_by_checker"] for r in out["rows"] if r["ranker"] == "legacy")
    assert legacy_violators > 0, "the unfiltered order leaks violators on these searches, which is what the filter removes"


def test_the_report_names_the_commit_and_says_when_the_code_that_ran_was_not_committed(monkeypatch):
    def fake_run(cmd, **_):
        if cmd[1] == "rev-parse":
            return type("R", (), {"stdout": "abc1234\n"})()
        return type("R", (), {"stdout": state["porcelain"]})()

    state = {"porcelain": ""}
    monkeypatch.setattr(D.subprocess, "run", fake_run)
    assert D.git_commit() == "abc1234"
    state["porcelain"] = "?? eval/domain_ranker.py\n"
    assert D.git_commit() == "abc1234 + uncommitted changes"


def test_slice_labels_follow_the_run_s_head_threshold_and_a_lift_over_almost_nothing_is_not_printed():
    users = _held_users(60)
    n = len(users)
    data = _analysis_data([2] * n, [5] * n, [60] * n, users, [100] * n)  # an extra system that never hits
    data["head_threshold"] = 37
    an = D.analyse(data, ["b0_reviews", "b1", "m2"], "dev", None, 20)
    meta = {"split": "dev", "fixture": "f.jsonl", "fixture_check": {"sha256": "ab" * 32, "verified": True}, "draws": 20,
            "head_threshold": 37, "git": "abc1234", "started": "now", "elapsed_s": 1, "limit": None,
            "hardware": {"processor": "x"}}
    text = D.render_report(meta, an, ["b0_reviews", "b1", "m2"], None, None, {"ran": False, "reason": "x"})
    assert "head target (37+ train reviews)" in text and "tail target (fewer than 37 train reviews)" in text
    assert "12+" not in text
    assert "n/a (baseline 0.0000)" in text, "M2 over a baseline that never hits would otherwise print an infinite lift"


def test_the_report_renders_for_a_partial_run_and_says_so():
    users = [u for u in range(1, 200) if u % 10 != 0][:60]
    users = [u for u in users for _ in range(4)]
    n = len(users)
    data = _analysis_data([2] * n, [5] * n, [60] * n, users)
    an = D.analyse(data, ["b1", "m2"], "dev", None, 50)
    meta = {"split": "dev", "fixture": "eval/product_dev_v1.jsonl", "fixture_check": {"sha256": "ab" * 32, "verified": True},
            "draws": 50, "head_threshold": 12, "git": "abc1234", "started": "2026-11-01 00:00 UTC", "elapsed_s": 60, "limit": 240,
            "hardware": {"processor": "test-cpu", "logical_cpus": 8, "ram_gb": 16.0}}
    text = D.render_report(meta, an, ["b1", "m2"], None, None, {"ran": False, "reason": "SERPAPI_KEY is not set"})
    assert "PARTIAL: the first 240 cases only" in text
    assert "target is MET" in text and "Not run: SERPAPI_KEY is not set." in text
    assert "Provider candidate recall (reported separately" in text and "Acceptance evidence" in text
    assert "Declared hardware" in text
