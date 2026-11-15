"""WP4 D5: the one-command evaluator for the product ranker.

    python -m eval.domain_ranker --domain product --fixture eval/product_dev_v1.jsonl \
        --systems generic classical domain_v1 --out artifacts/wp4_d5_dev

One pass over the evaluation cases scores every requested system and keeps, per case, the rank of the
real next item, each system's top 10 and the slice labels. Everything in the report is computed from that
per-case record, so a table can always be traced back to rows in ``per_case.csv`` / ``per_case.npz``.

System groups (the names the proposal's command uses)
    generic    B0, the order the product search had before WP4: with train-only ratings ("legacy") and
               with review counts only ("reviews only"), both free of end-of-period leakage.
    classical  B1 (BM25 + popularity, weight 0.25) and ItemKNN.
    domain_v1  M2, the constraint-first hybrid, at the weights the live ranker uses (LIVE_WEIGHTS).

What is reported
    * Recall / NDCG / MRR @10 on the held-out users (the 9 in 10 not used to tune M2's weights), overall
      and by slice: cold / warm, keyword-like / complex (history text cut at 51 words), head / tail target.
    * Relative lift over each baseline with a user-level bootstrap (2,000 resamples, seed 42) and the frozen
      decision rule for M2 over B1: lower bound above 0, point estimate of at least +3%, and the complex
      slice not worse than -2% at its lower bound.
    * Exposure: head share of the top 10, popularity of the top 10, catalogue coverage.
    * Price-parse and catalogue-mapping coverage of the candidate pools; whether the real item was retrieved
      (provider candidate recall, reported separately); metrics are conditional on retrieval and every system
      ranks the same pool.
    * The proposal's acceptance bullets, each marked PASS / FAIL from this run: the feature-off / no-network
      tests (run as pytest), zero hard violations, the unknown-price policy, M2 against the stronger of B0 / B1,
      complex-slice non-inferiority, head / tail exposure, p95 <= 25 ms on declared hardware, and a valid
      fallback for cold and sparse histories.
    * The 15 labelled searches in tests/wp4/cases: violators surfaced by B0 and by the constraint-first
      ranker, by constraint type and for items with no price.
    * Latency: ranker-only p50 / p95 on a dev sample, and the live ranker on 100 provider-shaped candidates.
    * A live smoke test, only when ``--live-smoke`` is given and SERPAPI_KEY is set. Otherwise it is
      recorded as "not run" with the reason.

Provider candidate recall is 100% by construction offline: every candidate pool contains the real item, so
there is no recall of the provider to measure. It is reported separately in the report and nothing else
depends on it.

The sealed test split is refused unless ``--open-test-split`` is given. Run it once, at the very end, with
the weights as they are: no further tuning.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import multiprocessing as mp
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

BACKEND_DIR = Path(__file__).resolve().parents[1]
for _p in (BACKEND_DIR, BACKEND_DIR / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import wp4_run_baselines as R  # noqa: E402  (B0 / B1 / ItemKNN scorers and the item cards)
from langgraph_metarec import product_ranker_m2 as M  # noqa: E402
from langgraph_metarec.product_ranker_live import LIVE_WEIGHTS  # noqa: E402

PROC_DIR = R.PROC_DIR
SPLIT_DIR = R.SPLIT_DIR

# ---------------------------------------------------------------------------
# What is being evaluated
# ---------------------------------------------------------------------------

GROUPS: Dict[str, List[str]] = {
    "generic": ["b0_legacy", "b0_reviews"],
    "classical": ["b1", "itemknn"],
    "domain_v1": ["m2"],
}
SYSTEM_LABELS: Dict[str, str] = {
    "b0_legacy": "B0 legacy (train-only ratings)",
    "b0_reviews": "B0 reviews only",
    "b1": "B1 (BM25 0.25 + popularity)",
    "itemknn": "ItemKNN",
    "m2": "M2 (domain_v1)",
}
B1_BM25_WEIGHT = 0.25
CUT_WORDS = 51            # history text longer than this is a "complex" case; fixed before any slice was scored
HEAD_PERCENTILE = 0.80    # head = the top 20% of items by train review count
TUNING_MOD = 10           # users with uid % 10 == 0 tuned M2's weights; they are never scored here
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 42
TARGET_LIFT = 0.03        # frozen decision rule: M2 over B1, NDCG@10
NON_INFERIORITY = -0.02   # frozen decision rule: lower bound on the complex slice
TOP_K = 10
MIN_BASELINE = 0.005      # below this metric value a relative lift is shown as n/a
DIRECTIONAL_LIFT = 0.05   # the proposal calls a 5% relative lift "directional", not the pass mark
P95_TARGET_MS = 25.0      # ranker-only p95 for 100 candidates
# the rule compares M2 with the stronger of B0 and B1 (the proposal's wording); ItemKNN is reported, not part of the rule
RULE_BASELINES = ("b0_legacy", "b0_reviews", "b1")

SLICES: List[Tuple[str, str]] = [
    ("all", "all held-out cases"),
    ("cold", "cold (no history)"),
    ("warm", "warm (has history)"),
    ("keyword", f"keyword-like (1 to {CUT_WORDS} words of history)"),
    ("complex", f"complex (more than {CUT_WORDS} words of history)"),
    ("head", "head target ({h}+ train reviews)"),          # {h} is filled with the run's head threshold
    ("tail", "tail target (fewer than {h} train reviews)"),
    ("no_price", "target price missing"),
]
METRICS = ("recall", "ndcg", "mrr", "gndcg")
METRIC_LABELS = {"recall": "Recall@10", "ndcg": "NDCG@10", "mrr": "MRR@10", "gndcg": "graded NDCG@10"}


def systems_for(groups: Sequence[str]) -> List[str]:
    chosen: List[str] = []
    for g in groups:
        for s in GROUPS[g]:
            if s not in chosen:
                chosen.append(s)
    return [s for s in GROUPS_ORDER if s in chosen]


GROUPS_ORDER = [s for g in GROUPS.values() for s in g]

# ---------------------------------------------------------------------------
# Splits: dev is open, test is sealed
# ---------------------------------------------------------------------------


def split_of(fixture: Path) -> str:
    name = fixture.name.lower()
    if "_test_" in name or name.startswith("test") or name.endswith("_test.jsonl"):
        return "test"
    return "dev"


def check_split_allowed(fixture: Path, open_test_split: bool) -> str:
    split = split_of(fixture)
    if split == "test" and not open_test_split:
        raise SystemExit(
            f"refusing to read {fixture.name}: it is the sealed test split. It is opened once, at the very end, "
            "with --open-test-split and no further tuning."
        )
    return split


def is_held_out(uid: int, split: str) -> bool:
    """Dev: the users M2's weights were not tuned on. Test: everyone (nothing is tuned on it)."""
    return True if split == "test" else (uid % TUNING_MOD) != 0


def count_splits(split: str) -> List[str]:
    """Interactions that count as the past for popularity and collaborative signals."""
    return ["train", "dev"] if split == "test" else ["train"]


# ---------------------------------------------------------------------------
# One worker's resources and the per-case scoring
# ---------------------------------------------------------------------------

G: Dict[str, Any] = {}


def load_resources(split: str) -> Dict[str, Any]:
    items = R.load_items()
    item_users: Dict[str, set] = {}
    for part in count_splits(split):
        with open(SPLIT_DIR / f"{part}.tsv", encoding="utf-8") as f:
            for line in f:
                user_id, asin, _ts = line.rstrip("\n").split("\t")
                item_users.setdefault(asin, set()).add(user_id)
    mean_rating: Dict[str, Optional[float]] = {}
    with open(PROC_DIR / "item_features_train.tsv", encoding="utf-8") as f:
        for line in f:
            asin, _n, mean = line.rstrip("\n").split("\t")
            mean_rating[asin] = float(mean) if mean else None
    for asin, meta in items.items():
        meta["rating_number"] = len(item_users.get(asin, ()))
        meta["average_rating"] = mean_rating.get(asin)
    counts = sorted(m["rating_number"] for m in items.values())
    return {
        "items": items,
        "iu": item_users,
        "head": counts[int(len(counts) * HEAD_PERCENTILE)],
        "index": {asin: i for i, asin in enumerate(items)},
    }


def _init_worker(split: str, systems: List[str]) -> None:
    G.update(load_resources(split))
    G["split"], G["systems"] = split, systems


def _order(scores: Sequence[float]) -> List[int]:
    # stable and descending, so a tie keeps candidate order; the same rule the earlier D2/D3 scripts used
    return sorted(range(len(scores)), key=scores.__getitem__, reverse=True)


def score_systems(case: dict, cards: List[dict], systems: Sequence[str], res: Dict[str, Any]) -> Dict[str, List[float]]:
    items, iu, head = res["items"], res["iu"], res["head"]
    hist = [h["parent_asin"] for h in case["history"]]
    out: Dict[str, List[float]] = {}
    for s in systems:
        if s == "b0_legacy":
            out[s] = R.b0_scores(cards)
        elif s == "b0_reviews":
            out[s] = R.b0_scores([{**c, "average_rating": None} for c in cards])
        elif s == "b1":
            out[s] = R.b1_scores(case["query"], cards, B1_BM25_WEIGHT)
        elif s == "itemknn":
            out[s] = R.itemknn_scores(hist, cards, iu)
        elif s == "m2":
            cats = {items[a]["main_category"] for a in hist if items.get(a, {}).get("main_category")}
            brands = {items[a]["brand"] for a in hist if items.get(a, {}).get("brand")}
            out[s] = [
                r.score
                for r in M.score_candidates(
                    history_asins=hist,
                    history_categories=cats,
                    history_brands=brands,
                    candidates=cards,
                    items=items,
                    item_users=iu,
                    head_threshold=head,
                    weights=LIVE_WEIGHTS,
                )
            ]
        else:  # pragma: no cover - guarded by the CLI
            raise ValueError(s)
    return out


def _work(args: Tuple[int, List[str]]) -> Dict[str, Any]:
    start, lines = args
    items, head, index, split, systems = G["items"], G["head"], G["index"], G["split"], G["systems"]
    n, S = len(lines), len(systems)
    out: Dict[str, Any] = {
        "start": start,
        "ranks": np.zeros((n, S), dtype=np.int16),
        "top": np.zeros((n, S, TOP_K), dtype=np.int8),
        "head10": np.zeros((n, S), dtype=np.int8),
        "pop10": np.zeros((n, S), dtype=np.float32),
        "uid": np.zeros(n, dtype=np.int64),
        "hist_words": np.zeros(n, dtype=np.int32),
        "cold": np.zeros(n, dtype=bool),
        "tcount": np.zeros(n, dtype=np.int32),
        "tpos": np.zeros(n, dtype=np.int16),
        "retrieved": np.zeros(n, dtype=bool),     # the real item is among the candidates
        "tpriced": np.zeros(n, dtype=bool),       # the real item has a parsed price
        "valid10": np.zeros((n, S), dtype=bool),  # a full top 10 of distinct candidates came back
        "constrained": np.zeros(n, dtype=bool),   # the case carries a machine-checkable constraint
        "n_pool": np.zeros(n, dtype=np.int16),
        "n_priced": np.zeros(n, dtype=np.int16),
        "n_mapped": np.zeros(n, dtype=np.int16),
        "cutoff": np.zeros(n, dtype=np.int64),
        "case_id": [],
        "user_key": [],
        "target": [],
    }
    shown: List[set] = [set() for _ in systems]
    for k, line in enumerate(lines):
        case = json.loads(line)
        ids = case["candidates"]
        cards = [R.item_card(a, items) for a in ids]
        target = case["ground_truth_item_ids"][0]
        retrieved = target in ids
        tpos = ids.index(target) if retrieved else -1
        uid = int(case["user_key"], 16) % (2**62)
        held = is_held_out(uid, split)
        counts = [c.get("rating_number") or 0 for c in cards]
        for si, (name, scores) in enumerate(score_systems(case, cards, systems, G).items()):
            order = _order(scores)
            # a target that was never retrieved cannot be ranked: it counts as a miss
            out["ranks"][k, si] = order.index(tpos) + 1 if retrieved else len(ids) + 1
            top = order[:TOP_K]
            out["valid10"][k, si] = len(top) == TOP_K and len(set(top)) == TOP_K
            out["top"][k, si, : len(top)] = top
            out["head10"][k, si] = sum(1 for i in top if counts[i] >= head)
            out["pop10"][k, si] = float(np.mean([math.log1p(counts[i]) for i in top]))
            if held:
                shown[si].update(index.get(ids[i], -1) for i in top)
        out["uid"][k] = uid
        out["hist_words"][k] = len(R.tokenize(case["query"]))
        out["cold"][k] = case["slice_labels"]["cold_warm"] == "cold"
        out["tcount"][k] = counts[tpos] if retrieved else 0
        out["tpos"][k] = tpos
        out["retrieved"][k] = retrieved
        out["tpriced"][k] = retrieved and not cards[tpos]["price_missing"]
        out["constrained"][k] = bool(case.get("strict_constraints"))
        out["n_pool"][k] = len(ids)
        out["n_priced"][k] = sum(1 for c in cards if not c["price_missing"])
        out["n_mapped"][k] = sum(1 for a in ids if a in items)
        out["cutoff"][k] = case["cutoff_time"]
        out["case_id"].append(case["case_id"])
        out["user_key"].append(case["user_key"])
        out["target"].append(target)
    out["shown"] = [np.fromiter(s, dtype=np.int64) for s in shown]
    return out


def read_batches(path: Path, limit: Optional[int], size: int = 1000) -> Iterable[Tuple[int, List[str]]]:
    buf: List[str] = []
    start = 0
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f):
            if limit is not None and n >= limit:
                break
            buf.append(line)
            if len(buf) == size:
                yield start, buf
                start += len(buf)
                buf = []
    if buf:
        yield start, buf


def run_pass(path: Path, split: str, systems: List[str], limit: Optional[int], workers: int) -> Dict[str, Any]:
    t0 = time.time()
    parts: List[Dict[str, Any]] = []
    done = 0
    with mp.Pool(workers, initializer=_init_worker, initargs=(split, systems)) as pool:
        for part in pool.imap(_work, read_batches(path, limit)):
            parts.append(part)
            done += len(part["case_id"])
            if done % 25000 < 1000:
                print(f"  {done:,} cases scored, {time.time() - t0:.0f}s", flush=True)
    data: Dict[str, Any] = {}
    for key in ("ranks", "top", "head10", "pop10", "uid", "hist_words", "cold", "tcount", "tpos", "retrieved",
                "tpriced", "valid10", "constrained", "n_pool", "n_priced", "n_mapped", "cutoff"):
        data[key] = np.concatenate([p[key] for p in parts])
    for key in ("case_id", "user_key", "target"):
        data[key] = [x for p in parts for x in p[key]]
    data["shown"] = [np.unique(np.concatenate([p["shown"][si] for p in parts])) for si in range(len(systems))]
    data["elapsed_s"] = time.time() - t0
    return data


def load_graded(split: str, data: Dict[str, Any]) -> Optional[np.ndarray]:
    """Graded label per case (5 stars = 2, 4 stars = 1, otherwise 0), checked row by row against the cases."""
    path = SPLIT_DIR / f"{split}_graded_relevance.tsv"
    if not path.exists():
        return None
    grade = np.zeros(len(data["case_id"]), dtype=np.int8)
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= len(grade):
                break
            _user, asin, ts, _rating, g = line.rstrip("\n").split("\t")
            if asin != data["target"][i] or int(ts) != int(data["cutoff"][i]):
                raise SystemExit(f"graded-relevance file is out of step with the cases at row {i + 1}")
            grade[i] = int(g)
    return grade


# ---------------------------------------------------------------------------
# Metrics and the bootstrap
# ---------------------------------------------------------------------------


def metric_arrays(rank: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    r = rank.astype(np.float64)
    hit = r <= TOP_K
    return hit.astype(float), np.where(hit, 1.0 / np.log2(r + 1), 0.0), np.where(hit, 1.0 / r, 0.0)


def slice_masks(data: Dict[str, Any], held: np.ndarray) -> Dict[str, np.ndarray]:
    h = data["hist_words"]
    head_t = data["tcount"] >= data["head_threshold"]
    cold = data["cold"]
    return {
        "all": held,
        "cold": held & cold,
        "warm": held & ~cold,
        "keyword": held & (h > 0) & (h <= CUT_WORDS),
        "complex": held & (h > CUT_WORDS),
        "head": held & head_t,
        "tail": held & ~head_t,
        "no_price": held & ~data["tpriced"],
    }


def per_case_metrics(data: Dict[str, Any], systems: Sequence[str], grade: Optional[np.ndarray]) -> Dict[str, Dict[str, np.ndarray]]:
    out: Dict[str, Dict[str, np.ndarray]] = {}
    for si, s in enumerate(systems):
        rc, nd, mr = metric_arrays(data["ranks"][:, si])
        out[s] = {"recall": rc, "ndcg": nd, "mrr": mr}
        if grade is not None:
            out[s]["gndcg"] = nd * (grade > 0)
    return out


def bootstrap_sums(uinv: np.ndarray, U: int, columns: np.ndarray, draws: int, seed: int, chunk: int = 100) -> np.ndarray:
    """Per-user sums of each column, then ``draws`` user-level resamples with replacement.

    Returns (draws, K): the resampled total of every column. A ratio of two totals from one resample is
    the resampled lift, so slices and systems share exactly the same resamples."""
    per_user = np.stack([np.bincount(uinv, weights=columns[:, k], minlength=U) for k in range(columns.shape[1])], axis=1)
    rng = np.random.default_rng(seed)
    out = np.zeros((draws, per_user.shape[1]))
    for lo in range(0, draws, chunk):
        hi = min(draws, lo + chunk)
        counts = np.stack([np.bincount(rng.integers(0, U, U), minlength=U) for _ in range(hi - lo)]).astype(np.float64)
        out[lo:hi] = counts @ per_user
    return out


def lift_interval(num: np.ndarray, den: np.ndarray, point_num: float, point_den: float) -> Tuple[float, float, float]:
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = num / den - 1.0
    rel = rel[np.isfinite(rel)]
    point = (point_num / point_den - 1.0) if point_den else float("nan")
    if not len(rel):  # an empty slice (a --limit smoke test can leave one) has nothing to resample
        return point, float("nan"), float("nan")
    lo, hi = np.percentile(rel, [2.5, 97.5])
    return point, float(lo), float(hi)


def analyse(data: Dict[str, Any], systems: List[str], split: str, grade: Optional[np.ndarray], draws: int) -> Dict[str, Any]:
    uid = data["uid"]
    held = np.array([is_held_out(int(u), split) for u in uid]) if split == "dev" else np.ones(len(uid), dtype=bool)
    # conditional metrics: only cases whose real item was retrieved are ranked, and every system sees the same pool
    masks = slice_masks(data, held & data["retrieved"])
    mets = per_case_metrics(data, systems, grade)
    metric_names = [m for m in METRICS if grade is not None or m != "gndcg"]

    held_idx = np.where(held)[0]
    users, uinv_h = np.unique(uid[held_idx], return_inverse=True)
    uinv = np.zeros(len(uid), dtype=np.int64)
    uinv[held_idx] = uinv_h
    U = len(users)

    # a column per (metric, slice, system); the bootstrap only ever sees held-out cases
    keys: List[Tuple[str, str, str]] = []
    cols: List[np.ndarray] = []
    for m in metric_names:
        for sl, mask in masks.items():
            for s in systems:
                keys.append((m, sl, s))
                cols.append((mets[s][m] * mask)[held_idx])
    boot = bootstrap_sums(uinv_h, U, np.stack(cols, axis=1), draws, BOOTSTRAP_SEED)
    col_of = {k: i for i, k in enumerate(keys)}
    point = {k: float(cols[i].sum()) for k, i in col_of.items()}

    table: Dict[str, Dict[str, Dict[str, float]]] = {}
    for sl, mask in masks.items():
        n = int(mask.sum())
        table[sl] = {
            "_n_cases": {"v": n},
            "_n_users": {"v": int(len(np.unique(uid[mask])))},
        }
        for s in systems:
            table[sl][s] = {m: (point[(m, sl, s)] / n if n else float("nan")) for m in metric_names}

    def lifts(a: str, b: str) -> Dict[str, Dict[str, Dict[str, float]]]:
        res: Dict[str, Dict[str, Dict[str, float]]] = {}
        for sl in masks:
            res[sl] = {}
            for m in metric_names:
                pt, lo, hi = lift_interval(
                    boot[:, col_of[(m, sl, a)]], boot[:, col_of[(m, sl, b)]],
                    point[(m, sl, a)], point[(m, sl, b)],
                )
                res[sl][m] = {"point": pt, "lo": lo, "hi": hi}
        return res

    comparisons: Dict[str, Any] = {}
    if "m2" in systems:
        for b in systems:
            if b != "m2":
                comparisons[f"m2_over_{b}"] = lifts("m2", b)

    exposure: Dict[str, Dict[str, float]] = {}
    catalogue = int(data["catalogue_size"])
    for si, s in enumerate(systems):
        sel = held
        exposure[s] = {
            "head_share_top10": float(data["head10"][sel, si].mean() / TOP_K),
            "mean_log_popularity_top10": float(data["pop10"][sel, si].mean()),
            "distinct_items_shown": int((data["shown"][si] >= 0).sum()),
            "catalogue_coverage": float((data["shown"][si] >= 0).sum() / catalogue),
        }
    pool = int(data["n_pool"].sum())
    cold = data["cold"]
    coverage = {
        "price_parsed_share": float(data["n_priced"].sum() / pool),
        "catalogue_mapped_share": float(data["n_mapped"].sum() / pool),
        "cases_with_no_priced_candidate": int((data["n_priced"] == 0).sum()),
        # provider candidate recall: how often the real item was in the candidate pool. Offline it is the
        # construction of the pool, so this is a check of the fixture, not a result about any ranker
        "provider_candidate_recall": float(data["retrieved"].mean()),
        "cases_target_not_retrieved": int((~data["retrieved"]).sum()),
        "cases_with_a_stated_constraint": int(data["constrained"].sum()),
        # a valid fallback: every system, cold and sparse users included, returns ten distinct candidates
        "valid_top10_share": {s: float(data["valid10"][:, si].mean()) for si, s in enumerate(systems)},
        "valid_top10_share_cold": {s: (float(data["valid10"][cold, si].mean()) if cold.any() else None) for si, s in enumerate(systems)},
        "valid_top10_share_sparse": {  # sparse = a warm user whose history text is ten words or fewer
            s: (float(data["valid10"][(data["hist_words"] > 0) & (data["hist_words"] <= 10), si].mean())
                if ((data["hist_words"] > 0) & (data["hist_words"] <= 10)).any() else None)
            for si, s in enumerate(systems)
        },
    }

    rule = None
    rule_pool = [s for s in RULE_BASELINES if s in systems]
    if "m2" in systems and rule_pool:
        baseline = max(rule_pool, key=lambda s: table["all"][s]["ndcg"])  # the stronger of B0 and B1
        overall = comparisons[f"m2_over_{baseline}"]["all"]["ndcg"]
        complex_ = comparisons[f"m2_over_{baseline}"]["complex"]["ndcg"]
        checks = {
            "lower_bound_above_zero": overall["lo"] > 0,
            "point_estimate_at_least_target": overall["point"] >= TARGET_LIFT,
            "complex_slice_non_inferior": complex_["lo"] > NON_INFERIORITY,
        }
        rule = {
            "baseline": baseline,
            "compared_with": rule_pool,
            "directional_5pct_reached": overall["point"] >= DIRECTIONAL_LIFT,
            "target_lift": TARGET_LIFT,
            "non_inferiority_margin": NON_INFERIORITY,
            "overall": overall,
            "complex": complex_,
            "checks": checks,
            "met": all(checks.values()),
        }
    strongest = max((s for s in systems if s != "m2"), key=lambda s: table["all"][s]["ndcg"], default=None)
    return {
        "n_cases_total": int(len(uid)),
        "n_cases_held_out": int(held.sum()),
        "n_users_held_out": int(U),
        "n_retrieved_held_out": int((held & data["retrieved"]).sum()),
        "table": table,
        "comparisons": comparisons,
        "exposure": exposure,
        "coverage": coverage,
        "rule": rule,
        "strongest_baseline": strongest,
        "metric_names": metric_names,
    }


# ---------------------------------------------------------------------------
# The labelled constraint searches (tests/wp4/cases)
# ---------------------------------------------------------------------------

LABEL_GROUPS = {"budget": "over budget", "brand": "wrong brand", "category": "wrong category",
                "compat": "wrong model / required term", "exclude": "wrong model / required term"}


CONSTRAINT_KINDS = ("budget", "brand", "model / category terms")


def constraint_types(c: Any) -> List[str]:
    """Which kinds of hard constraint a labelled search states."""
    kinds = []
    if c.budget_max is not None:
        kinds.append("budget")
    if c.brand_in or c.brand_not_in:
        kinds.append("brand")
    if c.category_terms or c.category_exclude_terms or c.require_terms or c.exclude_terms:
        kinds.append("model / category terms")
    return kinds


def constraint_section() -> Dict[str, Any]:
    from langgraph_metarec.graphs.generic_graph import _rank_items, normalize_tool_items
    from langgraph_metarec.product_constraints import parse_price
    from tests.wp4 import eval_constraints as EC
    from tests.wp4.dataset import load_cases

    cases = load_cases()
    report = EC.evaluate()  # the real graph with the constraint-first ranker on
    rows: List[Dict[str, Any]] = []
    by_type = {"generic": {}, "domain_v1": {}}
    for case, cr in zip(cases, report.cases):
        labels = case.labels_by_title()
        no_price = {c["title"] for c in case.pool if parse_price(c.get("price")) is None}
        b0_top = [it["title"] for it in _rank_items(normalize_tool_items("amazon.product.search", case.pool, "product"))[:10]]
        sides = {"generic": b0_top, "domain_v1": cr.surfaced_titles}
        row: Dict[str, Any] = {
            "id": case.id,
            "query": case.query,
            "types": constraint_types(case.hard_constraints),
            "pool": len(case.candidates),
            "pool_violators": len(case.pool_violator_titles),
            "unpriced_in_pool": len(no_price),
        }
        for side, titles in sides.items():
            bad = [t for t in titles if labels.get(t)]
            first = next((r for r, t in enumerate(titles, 1) if labels.get(t)), None)
            row[f"{side}_shown"] = len(titles)
            row[f"{side}_violators_top10"] = len(bad)
            row[f"{side}_violator_in_top3"] = bool(first and first <= 3)
            row[f"{side}_unpriced_shown"] = sum(1 for t in titles if t in no_price)
            for t in bad:
                for key in set(labels[t]):
                    group = LABEL_GROUPS.get(key, key)
                    by_type[side][group] = by_type[side].get(group, 0) + 1
        row["domain_v1_false_drops"] = max(0, min(cr.compliant_available - cr.policy_excluded, 10) - cr.surfaced_compliant)
        row["domain_v1_policy_excluded"] = cr.policy_excluded
        rows.append(row)
    total = {
        "searches": len(rows),
        "pool_violators": sum(r["pool_violators"] for r in rows),
        "generic_violators_top10": sum(r["generic_violators_top10"] for r in rows),
        "domain_v1_violators_top10": sum(r["domain_v1_violators_top10"] for r in rows),
        "generic_searches_with_top3_violation": sum(r["generic_violator_in_top3"] for r in rows),
        "domain_v1_searches_with_top3_violation": sum(r["domain_v1_violator_in_top3"] for r in rows),
        "unpriced_in_pools": sum(r["unpriced_in_pool"] for r in rows),
        "generic_unpriced_shown": sum(r["generic_unpriced_shown"] for r in rows),
        "domain_v1_unpriced_shown": sum(r["domain_v1_unpriced_shown"] for r in rows),
        "domain_v1_false_drops": sum(r["domain_v1_false_drops"] for r in rows),
        "domain_v1_policy_excluded": sum(r["domain_v1_policy_excluded"] for r in rows),
        "label_checker_disagreements": sum(len(c.label_checker_disagreements) for c in report.cases),
        "generic_slots_shown": sum(r["generic_shown"] for r in rows),
        "domain_v1_slots_shown": sum(r["domain_v1_shown"] for r in rows),
    }
    # violation rate two ways: the share of labelled violators that reach the top 10, and the share of shown slots
    total["generic_violation_rate"] = total["generic_violators_top10"] / total["pool_violators"] if total["pool_violators"] else 0.0
    total["domain_v1_violation_rate"] = total["domain_v1_violators_top10"] / total["pool_violators"] if total["pool_violators"] else 0.0
    total["generic_slot_violation_rate"] = total["generic_violators_top10"] / total["generic_slots_shown"] if total["generic_slots_shown"] else 0.0
    total["domain_v1_slot_violation_rate"] = total["domain_v1_violators_top10"] / total["domain_v1_slots_shown"] if total["domain_v1_slots_shown"] else 0.0
    kinds: Dict[str, Dict[str, Any]] = {}
    for kind in CONSTRAINT_KINDS:
        sel = [r for r in rows if kind in r["types"]]
        kinds[kind] = {
            "searches": len(sel),
            "pool_violators": sum(r["pool_violators"] for r in sel),
            "generic_violators_top10": sum(r["generic_violators_top10"] for r in sel),
            "domain_v1_violators_top10": sum(r["domain_v1_violators_top10"] for r in sel),
        }
    sel = [r for r in rows if r["unpriced_in_pool"]]
    kinds["missing price"] = {
        "searches": len(sel),
        "pool_violators": sum(r["unpriced_in_pool"] for r in sel),  # here: items with no price
        "generic_violators_top10": sum(r["generic_unpriced_shown"] for r in sel),
        "domain_v1_violators_top10": sum(r["domain_v1_unpriced_shown"] for r in sel),
    }
    return {"rows": rows, "total": total, "violators_by_type": by_type, "by_constraint_kind": kinds}


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------


def pct(xs: Sequence[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def latency_section(path: Path, res: Dict[str, Any], systems: List[str], n_cases: int, total_cases: int) -> Dict[str, Any]:
    items = res["items"]
    stride = max(1, total_cases // max(1, n_cases))
    prepared: List[Tuple[dict, List[dict], bool]] = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= total_cases or len(prepared) >= n_cases:
                break
            if i % stride:
                continue
            case = json.loads(line)
            prepared.append((case, [R.item_card(a, items) for a in case["candidates"]], bool(case["history"])))
    out: Dict[str, Any] = {"cases": len(prepared), "stride": stride, "systems": {}}
    for s in systems:
        for case, cards, _ in prepared[:20]:  # warm-up, not counted
            score_systems(case, cards, [s], res)
        times, warm = [], []
        for case, cards, has_hist in prepared:
            t0 = time.perf_counter()
            score_systems(case, cards, [s], res)
            ms = (time.perf_counter() - t0) * 1000
            times.append(ms)
            if has_hist:
                warm.append(ms)
        out["systems"][s] = {
            "p50_ms": pct(times, 0.5), "p95_ms": pct(times, 0.95), "max_ms": max(times),
            "warm_p95_ms": pct(warm, 0.95) if warm else None,
        }
    out["live_ranker_100"] = live_ranker_latency()
    return out


def live_ranker_latency(repeats: int = 100) -> Dict[str, Any]:
    """The live ranker (rank_live) on 100 provider-shaped candidates with a budget stated."""
    from langgraph_metarec.graphs.generic_graph import _rank_items, normalize_tool_items
    from langgraph_metarec.product_constraints import ProductConstraints
    from langgraph_metarec.product_ranker_live import rank_live

    pool = [
        {
            "title": f"Mechanical keyboard model {n}", "brand": None, "link": f"https://www.amazon.com/dp/B0{n:08d}",
            "rating": 4.4, "reviews": 10 + n * 7, "price": f"${20 + n}", "thumbnail": "https://example.test/t.jpg",
            "source": "amazon", "asin": f"B0{n:08d}",
        }
        for n in range(100)
    ]
    items = _rank_items(normalize_tool_items("amazon.product.search", pool, "product"))
    constraints = ProductConstraints(budget_max=100.0, currency="USD")
    times: List[float] = []
    for _ in range(repeats):
        out = rank_live(items=items, query="quiet mechanical keyboard for mac", preferences={"use_case": "Mac"}, constraints=constraints)
        times.append(out.result.latency_ms)
    return {"candidates": len(items), "repeats": repeats, "p50_ms": pct(times, 0.5), "p95_ms": pct(times, 0.95), "max_ms": max(times)}


# ---------------------------------------------------------------------------
# Live smoke test
# ---------------------------------------------------------------------------

SMOKE_QUERIES = 3


def live_smoke(enabled: bool) -> Dict[str, Any]:
    if not enabled:
        return {"ran": False, "reason": "not requested (pass --live-smoke)"}
    if not os.getenv("SERPAPI_KEY"):
        return {"ran": False, "reason": "SERPAPI_KEY is not set"}
    from langgraph_metarec.graphs.generic_graph import GenericGraphAdapters, run_generic_domain_graph
    from langgraph_metarec.product_constraints import violations
    from langgraph_metarec.tool_registry import build_default_tool_registry
    from tests.wp4 import eval_constraints as EC
    from tests.wp4.dataset import load_cases

    rows = []
    for case in load_cases()[:SMOKE_QUERIES]:
        for mode in ("legacy", "domain_v1"):
            with EC._product_ranker(mode):
                t0 = time.perf_counter()
                result = asyncio.run(
                    run_generic_domain_graph(
                        query=case.query, domain="product", preferences=case.preferences_with_constraints,
                        tool_tags=EC.PRODUCT_TAGS,
                        adapters=GenericGraphAdapters(tool_registry=build_default_tool_registry()),
                    )
                )
                ms = (time.perf_counter() - t0) * 1000
            top = result.items[:10]
            meta = result.metadata.get("product_ranker") or {}
            rows.append({
                "query": case.query, "ranker": mode, "returned": len(top), "total_ms": round(ms),
                "violators_by_checker": sum(1 for it in top if violations(it, case.hard_constraints)),
                "fell_back": bool(meta.get("fell_back")),
            })
    return {"ran": True, "rows": rows}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def fixture_check(path: Path, limit: Optional[int]) -> Dict[str, Any]:
    sums = path.parent / "checksums.sha256"
    expected = None
    if sums.exists():
        for line in sums.read_text(encoding="utf-8").splitlines():
            digest, _, name = line.strip().partition(" *")
            if name == path.name:
                expected = digest
    actual = sha256_of(path)
    return {"sha256": actual, "expected": expected, "verified": (expected == actual) if expected else None}


def hardware_info() -> Dict[str, Any]:
    """The machine the latency figures were measured on (the proposal asks for 'declared hardware')."""
    ram: Optional[float] = None
    try:
        import psutil  # type: ignore

        ram = round(psutil.virtual_memory().total / 2**30, 1)
    except Exception:
        try:  # Windows without psutil
            import ctypes

            class _Mem(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("tpage", ctypes.c_ulonglong), ("apage", ctypes.c_ulonglong),
                            ("tvirt", ctypes.c_ulonglong), ("avirt", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]

            mem = _Mem()
            mem.length = ctypes.sizeof(_Mem)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):  # type: ignore[attr-defined]
                ram = round(mem.total / 2**30, 1)
        except Exception:
            ram = None
    return {
        "processor": platform.processor() or platform.machine(),
        "logical_cpus": os.cpu_count(),
        "ram_gb": ram,
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
    }


# The proposal's acceptance evidence that is proved by tests rather than by the evaluation data: feature-off output
# equals the B0 strict-API fixtures, every ranked or excluded id comes from the input, nothing needs the network.
ACCEPTANCE_TESTS = [
    "tests/wp4/test_b0_golden.py",
    "tests/wp4/test_d4_live_ranker.py",
    "tests/wp4/test_wp4_baseline.py",
    "tests/wp4/test_product_ranker_m2.py",
]


def run_acceptance_tests() -> Dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if k != "SERPAPI_KEY"}  # they must pass with no provider key at all
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *ACCEPTANCE_TESTS, "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=BACKEND_DIR, capture_output=True, text=True, timeout=1200, env=env,
        )
    except Exception as err:  # pragma: no cover - a missing interpreter or a timeout
        return {"ran": False, "passed": False, "summary": f"could not run: {err}", "files": ACCEPTANCE_TESTS}
    lines = proc.stdout.strip().splitlines()
    return {"ran": True, "passed": proc.returncode == 0, "summary": lines[-1] if lines else proc.stderr[-200:], "files": ACCEPTANCE_TESTS}


def git_commit() -> Optional[str]:
    """The commit the code was run from; '+uncommitted changes' when the code that ran is not what that commit holds,
    so a report can never point at a commit that lacks the evaluator."""
    def git(*args: str) -> Optional[str]:
        try:
            return subprocess.run(["git", *args], cwd=BACKEND_DIR, capture_output=True, text=True, timeout=30).stdout
        except Exception:
            return None

    head = (git("rev-parse", "--short", "HEAD") or "").strip()
    if not head:
        return None
    code = ["eval", "langgraph_metarec", "scripts", "tests/wp4"]
    dirty = (git("status", "--porcelain", "--", *code) or "").strip()
    return f"{head} + uncommitted changes" if dirty else head


def write_per_case(out_dir: Path, data: Dict[str, Any], systems: List[str], grade: Optional[np.ndarray], held: np.ndarray) -> None:
    np.savez_compressed(
        out_dir / "per_case.npz",
        systems=np.array(systems), ranks=data["ranks"], top10_position=data["top"], head_in_top10=data["head10"],
        popularity_top10=data["pop10"], uid=data["uid"], held_out=held, history_words=data["hist_words"],
        cold=data["cold"], target_train_count=data["tcount"], target_position=data["tpos"],
        candidates_priced=data["n_priced"], candidates_mapped=data["n_mapped"], candidates_total=data["n_pool"],
        target_retrieved=data["retrieved"], target_price_known=data["tpriced"], valid_top10=data["valid10"],
        has_stated_constraint=data["constrained"],
        grade=grade if grade is not None else np.full(len(data["uid"]), -1, dtype=np.int8),
    )
    head_t = data["tcount"] >= data["head_threshold"]
    with open(out_dir / "per_case.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "user_key", "held_out", "cold_warm", "history_words", "complexity", "target_train_count",
                    "target_head_tail", "target_retrieved", "target_price_known", "has_stated_constraint", "graded"]
                   + [f"rank_{s}" for s in systems] + [f"valid_top10_{s}" for s in systems])
        for i, cid in enumerate(data["case_id"]):
            hw = int(data["hist_words"][i])
            w.writerow([
                cid, data["user_key"][i], int(held[i]), "cold" if data["cold"][i] else "warm", hw,
                "none" if hw == 0 else ("keyword" if hw <= CUT_WORDS else "complex"), int(data["tcount"][i]),
                "head" if head_t[i] else "tail", int(data["retrieved"][i]), int(data["tpriced"][i]),
                int(data["constrained"][i]), "" if grade is None else int(grade[i]),
            ] + [int(r) for r in data["ranks"][i]] + [int(v) for v in data["valid10"][i]])


def pc(x: float, digits: int = 2, sign: bool = False) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x * 100:+.{digits}f}%" if sign else f"{x * 100:.{digits}f}%"


def md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def acceptance_rows(an: Dict[str, Any], systems: List[str], constraint: Optional[Dict[str, Any]],
                    latency: Optional[Dict[str, Any]], acceptance: Optional[Dict[str, Any]]) -> List[List[str]]:
    """The proposal's five acceptance bullets, each marked from the numbers in this run (or 'not run')."""
    ok = lambda b: "PASS" if b else "FAIL"  # noqa: E731
    rows: List[List[str]] = []
    if acceptance and acceptance.get("ran"):
        rows.append(["Feature-off output equals the B0 strict-API fixtures; every ranked or excluded id comes from the input; no network",
                     f"pytest {', '.join(Path(f).name for f in acceptance['files'])}: {acceptance['summary']}", ok(acceptance["passed"])])
    else:
        rows.append(["Feature-off output equals the B0 strict-API fixtures; every ranked or excluded id comes from the input; no network",
                     "acceptance tests not run (`--skip-acceptance-tests`)", "NOT RUN"])
    if constraint:
        t = constraint["total"]
        rows.append(["Zero hard budget / brand / category / model violations in the labelled corpus",
                     f"{t['domain_v1_violators_top10']} of {t['pool_violators']} labelled violators surfaced (B0: {t['generic_violators_top10']})",
                     ok(t["domain_v1_violators_top10"] == 0)])
        rows.append(["Unknown price follows the declared strict policy (excluded when a budget is stated)",
                     f"{t['domain_v1_unpriced_shown']} unpriced items shown of {t['unpriced_in_pools']} in the pools; "
                     f"{t['domain_v1_policy_excluded']} compliant items excluded by the policy on purpose",
                     ok(t["domain_v1_unpriced_shown"] == 0)])
    else:
        rows.append(["Zero hard violations; unknown-price policy", "constraint corpus not run (`--skip-constraints`)", "NOT RUN"])
    rule = an["rule"]
    if rule:
        o, c = rule["overall"], rule["complex"]
        rows.append([f"M2 NDCG@10 exceeds the stronger of B0 / B1, which is {SYSTEM_LABELS[rule['baseline']]}, with a positive lower bound",
                     f"{pc(o['point'], sign=True)}, 95% [{pc(o['lo'], sign=True)}, {pc(o['hi'], sign=True)}]", ok(rule["checks"]["lower_bound_above_zero"])])
        rows.append([f"Frozen minimum effect {pc(TARGET_LIFT, 0, True)} on the point estimate", pc(o["point"], sign=True),
                     ok(rule["checks"]["point_estimate_at_least_target"])])
        rows.append([f"Directional reference: {pc(DIRECTIONAL_LIFT, 0, True)} relative lift (not the pass mark)", pc(o["point"], sign=True),
                     "reached" if rule["directional_5pct_reached"] else "not reached"])
        rows.append([f"Complex-query slice improves or is non-inferior (margin {pc(NON_INFERIORITY, 0, True)} on the lower bound)",
                     f"{pc(c['point'], sign=True)}, 95% [{pc(c['lo'], sign=True)}, {pc(c['hi'], sign=True)}]", ok(rule["checks"]["complex_slice_non_inferior"])])
    else:
        rows.append(["M2 against the stronger of B0 / B1; complex-slice non-inferiority", "needs `--systems generic classical domain_v1`", "NOT RUN"])
    if "m2" in an["exposure"]:
        e = an["exposure"]["m2"]
        rows.append(["Head / tail exposure reported, not only aggregate accuracy",
                     f"M2 head share of top 10 {pc(e['head_share_top10'], 1)}, catalogue coverage {pc(e['catalogue_coverage'], 1)} (section 4)", "reported"])
    if latency:
        m2 = latency["systems"].get("m2")
        lr = latency["live_ranker_100"]
        parts = [f"live ranker, 100 candidates: p95 {lr['p95_ms']:.1f} ms"]
        passed = lr["p95_ms"] <= P95_TARGET_MS
        if m2:
            parts.insert(0, f"offline M2 sample: p95 {m2['p95_ms']:.1f} ms")
            passed = passed and m2["p95_ms"] <= P95_TARGET_MS
        rows.append([f"Ranker-only p95 at most {P95_TARGET_MS:.0f} ms for 100 candidates on declared hardware", "; ".join(parts), ok(passed)])
    else:
        rows.append([f"Ranker-only p95 at most {P95_TARGET_MS:.0f} ms", "latency not run (`--skip-latency`)", "NOT RUN"])
    cov = an["coverage"]
    cold, sparse = cov["valid_top10_share_cold"], cov["valid_top10_share_sparse"]
    allv = cov["valid_top10_share"]
    good = all(v == 1.0 for v in allv.values()) and all(v in (None, 1.0) for v in cold.values()) and all(v in (None, 1.0) for v in sparse.values())
    rows.append(["Cold and sparse histories always return a valid text / popularity fallback (ten distinct candidates)",
                 "; ".join(f"{SYSTEM_LABELS[s]}: {pc(allv[s], 1)} of cases, cold {pc(cold[s], 1)}, sparse {pc(sparse[s], 1)}" for s in systems if s in ("b1", "m2")) or
                 "; ".join(f"{SYSTEM_LABELS[s]}: {pc(allv[s], 1)}" for s in systems), ok(good)])
    return rows


def render_report(meta: Dict[str, Any], an: Dict[str, Any], systems: List[str], constraint: Optional[Dict[str, Any]],
                  latency: Optional[Dict[str, Any]], smoke: Dict[str, Any], acceptance: Optional[Dict[str, Any]] = None) -> str:
    L: List[str] = []
    split_note = "SEALED TEST SPLIT, opened once with --open-test-split" if meta["split"] == "test" else "development split"
    L += [f"# WP4 D5 evaluation: product domain ({split_note})", ""]
    L += [md_table(["Item", "Value"], [
        ["Fixture", f"`{meta['fixture']}` (sha256 {meta['fixture_check']['sha256'][:16]}..., "
                    f"{'matches the frozen checksum' if meta['fixture_check']['verified'] else 'NOT verified against checksums.sha256'})"],
        ["Cases scored", f"{an['n_cases_total']:,} (held-out users: {an['n_cases_held_out']:,} cases, {an['n_users_held_out']:,} users)"],
        ["Systems", ", ".join(SYSTEM_LABELS[s] for s in systems)],
        ["M2 weights", ", ".join(f"{k} {v}" for k, v in LIVE_WEIGHTS.items())],
        ["Candidate pool", "real item + 99 random negatives per case (seed 42)"],
        ["Bootstrap", f"user-level, {meta['draws']:,} resamples, seed {BOOTSTRAP_SEED}, 95% percentile interval"],
        ["Head threshold", f"{meta['head_threshold']} train reviews (80th percentile)"],
        ["Code", f"git {meta['git'] or 'unknown'}, python {platform.python_version()}, {platform.system()}"],
        ["Run", f"{meta['started']}, scoring pass {meta['elapsed_s'] / 60:.1f} min"],
        ["Declared hardware", ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in meta["hardware"].items() if v)],
        ["Pools", f"every system ranks the same pool; metrics are conditional on the real item having been retrieved "
                  f"({an['n_retrieved_held_out']:,} of {an['n_cases_held_out']:,} held-out cases)"],
        *([["Scope", f"PARTIAL: the first {meta['limit']:,} cases only (a smoke test, not a result)"]] if meta["limit"] else []),
    ])]
    L += [""]

    rule = an["rule"]
    L += ["## 1. Decision rule (frozen before any result was seen)", ""]
    if rule:
        o, c = rule["overall"], rule["complex"]
        ok = lambda b: "PASS" if b else "FAIL"  # noqa: E731
        L += [md_table(["Criterion", "Result", "Status"], [
            [f"M2 over the stronger of B0 / B1, which is {SYSTEM_LABELS[rule['baseline']]}; NDCG@10 lower bound above 0", f"{pc(o['point'], sign=True)} [{pc(o['lo'], sign=True)}, {pc(o['hi'], sign=True)}]", ok(rule["checks"]["lower_bound_above_zero"])],
            [f"point estimate at least {pc(TARGET_LIFT, 0, True)}", pc(o["point"], sign=True), ok(rule["checks"]["point_estimate_at_least_target"])],
            [f"complex slice not worse than {pc(NON_INFERIORITY, 0, True)} (lower bound)", f"{pc(c['point'], sign=True)} [{pc(c['lo'], sign=True)}, {pc(c['hi'], sign=True)}]", ok(rule["checks"]["complex_slice_non_inferior"])],
        ])]
        L += ["", f"**Verdict: the {pc(TARGET_LIFT, 0, True)} target is {'MET' if rule['met'] else 'NOT MET'}.** "
                  f"The comparison is with the stronger of {', '.join(SYSTEM_LABELS[s] for s in rule['compared_with'])}; "
                  f"the strongest baseline overall, ItemKNN included, is {SYSTEM_LABELS[an['strongest_baseline']]}. "
                  f"The {pc(DIRECTIONAL_LIFT, 0, True)} relative lift the proposal calls directional is "
                  f"{'reached' if rule['directional_5pct_reached'] else 'not reached'}."]
    else:
        L += ["Not evaluated: it needs M2 and at least one of B0 / B1 (use `--systems generic classical domain_v1`)."]
    L += ["", "### Acceptance evidence (the proposal's bullets)", "",
          md_table(["Requirement", "This run", "Status"], acceptance_rows(an, systems, constraint, latency, acceptance))]

    L += ["", "## 2. Held-out users, all slices", ""]
    for sl, desc in SLICES:
        desc = desc.replace("{h}", str(meta["head_threshold"]))
        t = an["table"][sl]
        L += [f"### {desc}: {t['_n_cases']['v']:,} cases, {t['_n_users']['v']:,} users", ""]
        L += [md_table(["System"] + [METRIC_LABELS[m] for m in an["metric_names"]],
                       [[SYSTEM_LABELS[s]] + [f"{t[s][m]:.4f}" for m in an["metric_names"]] for s in systems]), ""]

    if an["comparisons"]:
        L += ["## 3. Relative lift of M2 (95% user-level bootstrap interval)", ""]
        for key, per_slice in an["comparisons"].items():
            base = key.replace("m2_over_", "")
            L += [f"### M2 over {SYSTEM_LABELS[base]}", ""]
            def cell(sl: str, m: str) -> str:
                if an["table"][sl][base][m] < MIN_BASELINE:  # a relative lift over almost nothing is not a number worth printing
                    return f"n/a (baseline {an['table'][sl][base][m]:.4f})"
                x = per_slice[sl][m]
                return f"{pc(x['point'], sign=True)} [{pc(x['lo'], sign=True)}, {pc(x['hi'], sign=True)}]"

            L += [md_table(["Slice"] + [METRIC_LABELS[m] for m in an["metric_names"]],
                           [[desc.replace("{h}", str(meta["head_threshold"]))] + [cell(sl, m) for m in an["metric_names"]]
                            for sl, desc in SLICES]), ""]

    L += ["## 4. Exposure and coverage (held-out users, top 10)", ""]
    L += [md_table(["System", "Head share of top 10", "Mean log(1+reviews)", "Distinct items shown", "Catalogue coverage"],
                   [[SYSTEM_LABELS[s], pc(an["exposure"][s]["head_share_top10"], 1), f"{an['exposure'][s]['mean_log_popularity_top10']:.2f}",
                     f"{an['exposure'][s]['distinct_items_shown']:,}", pc(an["exposure"][s]["catalogue_coverage"], 1)] for s in systems]), ""]

    cov = an["coverage"]
    L += ["## 5. Data coverage of the candidate pools", "",
          f"- Candidates with a parsed price: {pc(cov['price_parsed_share'], 1)}; cases where no candidate has a price: {cov['cases_with_no_priced_candidate']:,}.",
          f"- Candidates found in the item catalogue: {pc(cov['catalogue_mapped_share'], 1)}.",
          f"- Provider candidate recall (reported separately, not part of any ranker metric): {pc(cov['provider_candidate_recall'], 2)} "
          f"of cases had the real item in the pool ({cov['cases_target_not_retrieved']:,} did not). Offline this is true by construction: every pool is built around the real item, "
          "so it checks the fixture and says nothing about any provider.",
          f"- Cases that carry a machine-checkable `strict_constraints` entry: {cov['cases_with_a_stated_constraint']:,}. "
          "The offline cases state no budget, brand or model, so hard-constraint violation counts come from the labelled searches in section 6.", ""]

    L += ["## 6. Labelled constraint searches (tests/wp4/cases)", ""]
    if constraint:
        t = constraint["total"]
        L += [md_table(["", "B0 (no filter)", "domain_v1 (constraint-first)"], [
            ["Labelled violators in the top 10", f"{t['generic_violators_top10']} of {t['pool_violators']}", f"{t['domain_v1_violators_top10']} of {t['pool_violators']}"],
            ["Violation rate: share of labelled violators that reach the top 10", pc(t["generic_violation_rate"], 1), pc(t["domain_v1_violation_rate"], 1)],
            ["Violation rate: share of shown slots held by a violator", pc(t["generic_slot_violation_rate"], 1), pc(t["domain_v1_slot_violation_rate"], 1)],
            ["Searches with a violator in the top 3", f"{t['generic_searches_with_top3_violation']} of {t['searches']}", f"{t['domain_v1_searches_with_top3_violation']} of {t['searches']}"],
            ["Unpriced items shown", t["generic_unpriced_shown"], t["domain_v1_unpriced_shown"]],
        ]), "",
            f"Compliant items dropped by mistake: {t['domain_v1_false_drops']}. Compliant items excluded on purpose by the missing-price policy: {t['domain_v1_policy_excluded']}. "
            f"Unpriced items in the 15 pools: {t['unpriced_in_pools']}. Label versus checker disagreements: {t['label_checker_disagreements']}.", ""]
        types = sorted(set(constraint["violators_by_type"]["generic"]) | set(constraint["violators_by_type"]["domain_v1"]))
        L += [md_table(["Violation type", "B0", "domain_v1"],
                       [[ty, constraint["violators_by_type"]["generic"].get(ty, 0), constraint["violators_by_type"]["domain_v1"].get(ty, 0)] for ty in types]), ""]
        L += ["Sliced by the kind of constraint a search states (a search can state several), and by missing price "
              "(for that row the counts are unpriced items, not violators):", ""]
        L += [md_table(["Constraint kind", "Searches", "Pool violators / unpriced", "B0 top 10", "domain_v1 top 10"],
                       [[k, v["searches"], v["pool_violators"], v["generic_violators_top10"], v["domain_v1_violators_top10"]]
                        for k, v in constraint["by_constraint_kind"].items()]), ""]
        L += [md_table(["Search", "Constraints", "Pool violators", "B0 top 10", "domain_v1 top 10", "Unpriced in pool"],
                       [[r["id"], ", ".join(r["types"]), r["pool_violators"], r["generic_violators_top10"], r["domain_v1_violators_top10"], r["unpriced_in_pool"]]
                        for r in constraint["rows"]]), ""]
    else:
        L += ["Not run (`--skip-constraints`).", ""]

    L += ["## 7. Latency", ""]
    if latency:
        L += [f"Ranker-only, one process, {latency['cases']} dev cases (every {latency['stride']}th case), 100 candidates each, on the hardware declared at the top.", ""]
        L += [md_table(["System", "p50 ms", "p95 ms", "max ms", "p95 ms, users with history"],
                       [[SYSTEM_LABELS[s], f"{v['p50_ms']:.2f}", f"{v['p95_ms']:.2f}", f"{v['max_ms']:.2f}",
                         "n/a" if v["warm_p95_ms"] is None else f"{v['warm_p95_ms']:.2f}"] for s, v in latency["systems"].items()]), ""]
        lr = latency["live_ranker_100"]
        L += [f"Live ranker (`rank_live`) on {lr['candidates']} provider-shaped candidates with a budget stated, {lr['repeats']} runs: "
              f"p50 {lr['p50_ms']:.2f} ms, p95 {lr['p95_ms']:.2f} ms, max {lr['max_ms']:.2f} ms. The target is 25 ms at p95.", ""]
    else:
        L += ["Not run (`--skip-latency`).", ""]

    L += ["## 8. Live smoke test", ""]
    if smoke["ran"]:
        L += [md_table(["Query", "Ranker", "Returned", "Violators (checker)", "Fell back", "Total ms"],
                       [[r["query"], r["ranker"], r["returned"], r["violators_by_checker"], r["fell_back"], r["total_ms"]] for r in smoke["rows"]]), ""]
    else:
        L += [f"Not run: {smoke['reason']}.", ""]
    L += ["## 9. Reading the numbers", "",
          "- Case construction: a case is one real interaction; the candidates are that item plus 99 random items the user never touched. "
          "Popularity is the train review count, so nothing from the dev period or later leaks in.",
          "- The ranker weights were tuned on the tuning users only and are not changed here. Sections 1 to 4 are on users the tuning never saw.",
          "- The offline cases carry no stated budget, brand or model, so the constraint gate is a no-op in sections 2 to 5; section 6 is where it is tested.",
          "- Per-case artifacts: `per_case.csv` (rank of the real item for every system, slice labels, retrieved flag, valid-top-10 flag) and "
          "`per_case.npz` (the same plus each system's top 10 as positions in the case's candidate list, so any top 10 can be rebuilt from the fixture).", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m eval.domain_ranker", description=__doc__.split("\n\n")[0])
    p.add_argument("--domain", required=True, choices=["product"])
    p.add_argument("--fixture", required=True, type=Path)
    p.add_argument("--systems", nargs="+", required=True, choices=sorted(GROUPS))
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--limit", type=int, default=None, help="score only the first N cases (a smoke test)")
    p.add_argument("--workers", type=int, default=max(1, min(7, (os.cpu_count() or 2) - 1)))
    p.add_argument("--bootstrap-draws", type=int, default=BOOTSTRAP_DRAWS)
    p.add_argument("--latency-cases", type=int, default=500)
    p.add_argument("--skip-latency", action="store_true")
    p.add_argument("--skip-constraints", action="store_true")
    p.add_argument("--skip-acceptance-tests", action="store_true", help="do not run the pytest files that prove the feature-off / no-network bullets")
    p.add_argument("--live-smoke", action="store_true", help="also call the live search; needs SERPAPI_KEY")
    p.add_argument("--open-test-split", action="store_true", help="allow the sealed test split; run once, at the very end")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    fixture = args.fixture if args.fixture.is_absolute() else (BACKEND_DIR / args.fixture)
    if not fixture.exists():
        raise SystemExit(f"fixture not found: {fixture}")
    split = check_split_allowed(fixture, args.open_test_split)
    systems = systems_for(args.systems)
    args.out.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[D5] {fixture.name} ({split}), systems: {', '.join(systems)}, workers {args.workers}", flush=True)

    fx = fixture_check(fixture, args.limit)
    print(f"[D5] fixture sha256 {fx['sha256'][:16]}... verified={fx['verified']}", flush=True)

    data = run_pass(fixture, split, systems, args.limit, args.workers)
    res = load_resources(split)  # head threshold and catalogue size for the report, built the same way as in the workers
    data["head_threshold"], data["catalogue_size"] = res["head"], len(res["items"])
    grade = load_graded(split, data)
    held = np.array([is_held_out(int(u), split) for u in data["uid"]])

    an = analyse(data, systems, split, grade, args.bootstrap_draws)
    constraint = None if args.skip_constraints else constraint_section()
    latency = None if args.skip_latency else latency_section(fixture, res, systems, args.latency_cases, len(data["case_id"]))
    # after the latency measurement, so the test run cannot disturb it
    acceptance = None if args.skip_acceptance_tests else run_acceptance_tests()
    smoke = live_smoke(args.live_smoke)

    meta = {"split": split, "fixture": str(args.fixture).replace("\\", "/"), "fixture_check": fx, "draws": args.bootstrap_draws,
            "head_threshold": data["head_threshold"], "git": git_commit(), "started": started, "elapsed_s": data["elapsed_s"],
            "limit": args.limit, "hardware": hardware_info()}
    write_per_case(args.out, data, systems, grade, held)
    (args.out / "report.md").write_text(render_report(meta, an, systems, constraint, latency, smoke, acceptance), encoding="utf-8")
    (args.out / "report.json").write_text(
        json.dumps({"meta": meta, "systems": systems, "analysis": an, "constraints": constraint, "latency": latency,
                    "acceptance_tests": acceptance, "live_smoke": smoke},
                   indent=2, default=lambda o: o.item() if hasattr(o, "item") else str(o)),
        encoding="utf-8",
    )
    print(f"[D5] wrote {args.out}/report.md, report.json, per_case.csv, per_case.npz", flush=True)
    if an["rule"]:
        print(f"[D5] decision rule: {'MET' if an['rule']['met'] else 'NOT MET'} "
              f"(M2 over {an['rule']['baseline']} NDCG@10 {pc(an['rule']['overall']['point'], sign=True)})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
