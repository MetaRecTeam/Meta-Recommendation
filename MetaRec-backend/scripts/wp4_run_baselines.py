"""WP4 D2 — run B0 and B1 over the frozen eval cases and report metrics.

B0 = the existing legacy order, reproduced on this dataset: sort candidates
by (average_rating, rating_number, title), descending. This is the same
formula frozen in D0 (`generic_graph._item_score`); the "popularity" term
from that formula is dropped here because D0 already found it is always 0
for Amazon products (the field never exists in `raw`), so including it would
be dead weight, not a faithful reproduction gap.

B1 = BM25(query, candidate title) + calibrated popularity, where:
  - query = the user's history-profile text built by wp4_build_eval_cases.py
    (empty for cold users)
  - popularity = log1p(rating_number), a standard way to stop a handful of
    massively-reviewed items from dominating raw review counts
  - BM25 and popularity are each z-score normalised WITHIN the case's 100
    candidates before being summed, so the two signals are on a comparable
    scale per case rather than one swamping the other by raw magnitude.
  - Cold users (empty query) have no text to score, so B1 falls back to
    popularity alone for them.

ItemKNN = the "one feasible collaborative baseline" the proposal's D2 line
asks for, added per Yanny's conditional ("add ItemKNN only if D1 shows the
interaction data supports it" — checked separately: train has a median of
54 co-occurring items per item, only 0.6% of items fully isolated, so the
data does support it). Pure collaborative signal, no text at all:
  - item-item cosine similarity, computed from which TRAIN users interacted
    with each item: sim(i, j) = |users(i) & users(j)| / sqrt(|users(i)| * |users(j)|)
  - a candidate's score = average similarity to every item in the user's
    (already-capped-to-20) history, matching the same history window B1 uses
  - cold users (no history) fall back to popularity alone, same policy as B1

Metrics: Recall@10, NDCG@10, MRR@10. Since each case has exactly one
relevant item (the ground truth), NDCG@10 simplifies to 1/log2(rank+1) when
rank<=10 else 0 (IDCG is always 1 for a single relevant item), and MRR@10 to
1/rank when rank<=10 else 0. Reported overall and split by cold/warm.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

from rank_bm25 import BM25Okapi

SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
from wp4_dataset_parser import parse_item_price  # noqa: E402

EVAL_DIR = BACKEND_DIR / "eval"
PROC_DIR = BACKEND_DIR / "data" / "wp4" / "processed"
SPLIT_DIR = PROC_DIR / "split"
ITEMS_JSONL = PROC_DIR / "items_5core.jsonl"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


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


def b0_scores(candidates: List[dict]) -> List[float]:
    # Sort key = (rating, rating_number, title) descending. Returned as a
    # single float per candidate is awkward for a 3-part tiebreak, so this
    # returns a rank-position-based score instead: the candidate's index in
    # the properly sorted order, inverted, which preserves the exact same
    # ordering as the real tuple sort.
    order = sorted(
        range(len(candidates)),
        key=lambda i: (
            candidates[i].get("average_rating") or 0,
            candidates[i].get("rating_number") or 0,
            candidates[i].get("title") or "",
        ),
        reverse=True,
    )
    scores = [0.0] * len(candidates)
    for rank, idx in enumerate(order):
        scores[idx] = -float(rank)  # higher (less negative) = better
    return scores


def b1_scores(query: str, candidates: List[dict], bm25_weight: float = 0.5) -> List[float]:
    pop = [math.log1p(c.get("rating_number") or 0) for c in candidates]
    pop_z = zscore(pop)

    tokens = tokenize(query)
    if not tokens:
        # Cold user: no query text, fall back to popularity alone.
        return pop_z

    corpus = [tokenize(c.get("title") or "") for c in candidates]
    bm25 = BM25Okapi(corpus)
    bm25_raw = list(bm25.get_scores(tokens))
    bm25_z = zscore(bm25_raw)
    return [bm25_weight * b + (1 - bm25_weight) * p for b, p in zip(bm25_z, pop_z)]


def load_items() -> Dict[str, dict]:
    items: Dict[str, dict] = {}
    with open(ITEMS_JSONL, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            items[obj["parent_asin"]] = obj
    return items


def item_card(asin: str, items: Dict[str, dict]) -> dict:
    """Candidate-facing fields, with provenance/missingness, per the
    proposal's "candidate features include provenance and missingness"
    requirement. Built on demand at scoring time — the eval case file only
    stores the ASIN, not a copy of this card, to keep the file size sane.

    Price uses the real parser (wp4_dataset_parser.parse_item_price), not a
    naive `price is None` check — D1's own missingness report found 160
    items hold the literal string "-" instead of null, which a naive check
    misses. Reusing the real parser keeps this consistent with that
    already-corrected number (26,846 / 33.5%), not the stale one.
    """
    meta = items.get(asin, {})
    parsed_price = parse_item_price(meta.get("price"))
    return {
        "parent_asin": asin,
        "title": meta.get("title"),
        "price": parsed_price.amount,
        "price_missing": parsed_price.amount is None,
        "price_provenance": parsed_price.provenance,
        "average_rating": meta.get("average_rating"),
        "rating_number": meta.get("rating_number"),
        "brand": meta.get("brand"),
        "brand_missing": not meta.get("brand"),
        "main_category": meta.get("main_category"),
        "main_category_missing": not meta.get("main_category"),
    }


def load_item_users() -> Dict[str, Set[str]]:
    """item_id -> set of train user_ids who interacted with it. The basis for
    ItemKNN's cosine similarity — built once per run, shared across all cases."""
    item_users: Dict[str, Set[str]] = {}
    with open(SPLIT_DIR / "train.tsv", encoding="utf-8") as f:
        for line in f:
            user_id, asin, _ts = line.rstrip("\n").split("\t")
            item_users.setdefault(asin, set()).add(user_id)
    return item_users


def _cosine_sim(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / math.sqrt(len(a) * len(b))


def itemknn_scores(history_asins: List[str], candidates: List[dict], item_users: Dict[str, Set[str]]) -> List[float]:
    pop = [math.log1p(c.get("rating_number") or 0) for c in candidates]
    pop_z = zscore(pop)

    if not history_asins:
        # Cold user: no history to compare against, fall back to popularity.
        return pop_z

    history_user_sets = [item_users.get(a, set()) for a in history_asins]
    history_user_sets = [s for s in history_user_sets if s]  # drop history items never seen in train
    if not history_user_sets:
        return pop_z

    scores = []
    for c in candidates:
        c_users = item_users.get(c["parent_asin"], set())
        sims = [_cosine_sim(c_users, h_users) for h_users in history_user_sets]
        scores.append(statistics.fmean(sims))
    return scores


def rank_of_target(scores: List[float], candidates: List[dict], target_asin: str) -> int:
    order = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)
    for rank, idx in enumerate(order, start=1):
        if candidates[idx]["parent_asin"] == target_asin:
            return rank
    return len(candidates) + 1  # not found (shouldn't happen; target is always a candidate)


def metrics_for_rank(rank: int, k: int = 10) -> dict:
    hit = rank <= k
    return {
        "recall": 1.0 if hit else 0.0,
        "ndcg": (1.0 / math.log2(rank + 1)) if hit else 0.0,
        "mrr": (1.0 / rank) if hit else 0.0,
    }


def run(system: str, split: str, limit: Optional[int], bm25_weight: float = 0.5) -> None:
    path = EVAL_DIR / f"product_{split}_v1.jsonl"
    if not path.exists():
        sys.exit(f"missing {path} — run wp4_build_eval_cases.py --split {split} first")

    items = load_items()
    item_users = load_item_users() if system == "itemknn" else None

    totals = {"all": [], "warm": [], "cold": []}
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if limit is not None and n >= limit:
                break
            case = json.loads(line)
            n += 1
            candidates = [item_card(a, items) for a in case["candidates"]]
            target = case["ground_truth_item_ids"][0]
            slice_label = case["slice_labels"]["cold_warm"]

            if system == "b0":
                scores = b0_scores(candidates)
            elif system == "b1":
                scores = b1_scores(case["query"], candidates, bm25_weight)
            elif system == "itemknn":
                history_asins = [h["parent_asin"] for h in case["history"]]
                scores = itemknn_scores(history_asins, candidates, item_users)
            else:
                sys.exit("system must be 'b0', 'b1' or 'itemknn'")

            rank = rank_of_target(scores, candidates, target)
            m = metrics_for_rank(rank)
            totals["all"].append(m)
            totals[slice_label].append(m)

            if n % 50000 == 0:
                print(f"[wp4_run_baselines] {system}/{split}: {n:,} cases scored ...", flush=True)

    weight_note = f", bm25_weight={bm25_weight}" if system == "b1" else ""
    print(f"\n=== {system.upper()} on {split} ({n:,} cases{weight_note}) ===")
    for label in ("all", "warm", "cold"):
        rows = totals[label]
        if not rows:
            continue
        recall = statistics.fmean(r["recall"] for r in rows)
        ndcg = statistics.fmean(r["ndcg"] for r in rows)
        mrr = statistics.fmean(r["mrr"] for r in rows)
        print(f"  {label:5s} (n={len(rows):>7,}): Recall@10={recall:.4f}  NDCG@10={ndcg:.4f}  MRR@10={mrr:.4f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--system", required=True, choices=["b0", "b1", "itemknn"])
    parser.add_argument("--split", required=True, choices=["dev", "test"])
    parser.add_argument("--limit", type=int, default=None, help="only score the first N cases (for a quick check)")
    parser.add_argument("--bm25-weight", type=float, default=0.5, help="weight on the BM25 term vs popularity (b1 only)")
    args = parser.parse_args()
    run(args.system, args.split, args.limit, args.bm25_weight)
