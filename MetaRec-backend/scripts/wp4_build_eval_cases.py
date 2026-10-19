"""WP4 D2 — build B1/M2 evaluation cases from the frozen D1 dataset.

The frozen dataset (``data/wp4/processed/``) is just raw interaction rows
(user_id, parent_asin, timestamp) and a separate item catalogue. Neither B1
(BM25 + popularity) nor M2 can be scored against that directly — there is no
"query", no candidate pool, and no notion of "what should rank #1". This
script turns the raw rows into the ``DomainEvalCaseV1``-shaped cases the
proposal's evaluation interface expects (one JSONL row per prediction task).

Design (recorded here since none of it is dictated by the proposal itself —
these are judgment calls, not requirements):

* One eval case per interaction in the target split (dev or test), not one
  per user. A user with 3 dev interactions produces 3 cases.
* History boundary: dev cases use TRAIN only as history. Test cases use
  TRAIN + DEV as history (dev is chronologically "past" relative to test).
* "Query" is synthesized, not typed by a real person: the user's own most
  recent 20 history item titles, concatenated. Users with zero history in
  the boundary set are "cold" and get an empty query (no profile text
  exists to match against) — B1 must fall back to pure popularity for them.
* Candidate pool: the true target item + 99 randomly sampled items the user
  never interacted with across train+dev+test combined (100 candidates
  total). This is a convention from offline recommender evaluation
  (consistent Recall@10/NDCG@10/MRR@10), not a proposal requirement.
* Negative sampling is seeded (default 42) so the cases are reproducible —
  same seed, same output, checksummed like the rest of D1/D2.

What this does NOT do: real text queries, strict_constraints, or
complex/keyword query slicing. Those come from the live SerpApi-shaped
eval (tests/wp4/cases/), not from this offline interaction dataset. The
only slice label this script can honestly produce from this data is
cold/warm user.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROC_DIR = BACKEND_DIR / "data" / "wp4" / "processed"
SPLIT_DIR = PROC_DIR / "split"
ITEMS_JSONL = PROC_DIR / "items_5core.jsonl"
EVAL_DIR = BACKEND_DIR / "eval"

HISTORY_CAP = 20
NUM_NEGATIVES = 99


def log(msg: str) -> None:
    print(f"[wp4_build_eval_cases] {msg}", flush=True)


def _read_interactions(path: Path) -> List[Tuple[str, str, int]]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            user_id, asin, ts = line.rstrip("\n").split("\t")
            rows.append((user_id, asin, int(ts)))
    return rows


def _load_items() -> Dict[str, dict]:
    items: Dict[str, dict] = {}
    with open(ITEMS_JSONL, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            items[obj["parent_asin"]] = obj
    return items


def build(split: str, seed: int, history_cap: int, num_negatives: int) -> None:
    if split not in ("dev", "test"):
        sys.exit("split must be 'dev' or 'test'")

    train_path = SPLIT_DIR / "train.tsv"
    target_path = SPLIT_DIR / f"{split}.tsv"
    if not train_path.exists() or not target_path.exists():
        sys.exit(f"missing {train_path} or {target_path} — run wp4_freeze_dataset.py --stage split first")

    log(f"loading item catalogue from {ITEMS_JSONL} ...")
    items = _load_items()
    all_asins = list(items.keys())
    n_items = len(all_asins)
    log(f"  {n_items:,} items")

    log("loading train.tsv (always part of history) ...")
    train_rows = _read_interactions(train_path)
    history_rows = list(train_rows)

    if split == "test":
        log("loading dev.tsv (history boundary extends to train+dev for test cases) ...")
        history_rows += _read_interactions(SPLIT_DIR / "dev.tsv")

    log(f"loading {split}.tsv (the target interactions to build cases from) ...")
    target_rows = _read_interactions(target_path)

    # Per-user chronological history, from the boundary set only.
    history_by_user: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for user_id, asin, ts in history_rows:
        history_by_user[user_id].append((asin, ts))
    for user_id in history_by_user:
        history_by_user[user_id].sort(key=lambda pair: pair[1])

    # Full per-user exclusion set for negative sampling: everything this user
    # ever touched across train+dev+test, so a "negative" is never something
    # they actually interacted with anywhere.
    log("building full per-user exclusion sets (train+dev+test) for negative sampling ...")
    all_rows = train_rows + _read_interactions(SPLIT_DIR / "dev.tsv") + _read_interactions(SPLIT_DIR / "test.tsv")
    touched_by_user: Dict[str, set] = defaultdict(set)
    for user_id, asin, _ts in all_rows:
        touched_by_user[user_id].add(asin)

    rng = random.Random(seed)
    n_cold = 0
    n_warm = 0
    out_path = EVAL_DIR / f"product_{split}_v1.jsonl"
    EVAL_DIR.mkdir(parents=True, exist_ok=True)

    log(f"building eval cases for {len(target_rows):,} {split} interactions ...")
    with open(out_path, "w", encoding="utf-8") as out:
        for idx, (user_id, target_asin, target_ts) in enumerate(target_rows):
            history = history_by_user.get(user_id, [])
            is_cold = len(history) == 0
            if is_cold:
                n_cold += 1
                query_text = ""
                history_card = []
            else:
                n_warm += 1
                recent = history[-history_cap:]
                # Store only (asin, timestamp) — not title — to keep the file
                # small. Titles are looked up from items_5core.jsonl at
                # scoring time (it's loaded there anyway); query_text itself
                # is still built from the real titles right here, so nothing
                # about the actual B1 profile text changes.
                history_card = [{"parent_asin": asin, "timestamp": ts} for asin, ts in recent]
                query_text = " ".join(str(items.get(asin, {}).get("title") or "") for asin, _ts in recent)

            excluded = touched_by_user.get(user_id, set())
            # Rejection sampling, not a full 80k-item filter per row: excluded
            # sets are small (a handful to a few dozen items per user), so
            # drawing random indices and skipping collisions is O(num_negatives)
            # in practice, not O(len(all_asins)) per row.
            if n_items - len(excluded) < num_negatives:
                sys.exit(f"user {user_id} has too few uninteracted items to sample {num_negatives} negatives")
            negatives: List[str] = []
            seen = {target_asin}
            while len(negatives) < num_negatives:
                candidate = all_asins[rng.randrange(n_items)]
                if candidate in excluded or candidate in seen:
                    continue
                seen.add(candidate)
                negatives.append(candidate)

            candidate_asins = negatives + [target_asin]
            rng.shuffle(candidate_asins)  # avoid target always landing in a fixed position
            # Store just the 100 ASINs, not each one's full metadata — that
            # metadata already lives once in items_5core.jsonl; duplicating
            # title/price/rating/brand/category into every case (100x over)
            # is what blew the file up to 20GB+ the first time. Metadata is
            # looked up from the catalogue at scoring time instead.

            case = {
                "case_id": f"{split}_{idx:07d}",
                "user_key": hashlib.sha1(user_id.encode("utf-8")).hexdigest()[:16],
                "cutoff_time": target_ts,
                "query": query_text,
                "fused_preferences": {},
                "history": history_card,
                "candidates": candidate_asins,
                "ground_truth_item_ids": [target_asin],
                "strict_constraints": {},
                "slice_labels": {"cold_warm": "cold" if is_cold else "warm"},
            }
            out.write(json.dumps(case) + "\n")

    log(f"wrote {len(target_rows):,} cases to {out_path}")
    log(f"  warm (has train-boundary history): {n_warm:,} ({n_warm / len(target_rows):.1%})")
    log(f"  cold (no history): {n_cold:,} ({n_cold / len(target_rows):.1%})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", required=True, choices=["dev", "test"])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--history-cap", type=int, default=HISTORY_CAP)
    parser.add_argument("--num-negatives", type=int, default=NUM_NEGATIVES)
    args = parser.parse_args()
    build(args.split, args.seed, args.history_cap, args.num_negatives)
