"""WP4 D2: one pass over the dev cases, leak-safe popularity (train counts).
Stores the rank of the real next item under B0 (reviews-only), ItemKNN and B1 at a grid of BM25 weights.
Run from MetaRec-backend/:  .venv/Scripts/python.exe scripts/wp4_sweep_ranks.py [limit|all] [out.npz]
"""
import json
import math
import multiprocessing as mp
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts")
sys.path.insert(0, ".")
import wp4_run_baselines as R  # noqa: E402
from rank_bm25 import BM25Okapi  # noqa: E402

WEIGHTS = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60, 0.80, 1.0]
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("sweep_ranks.npz")
G = {}


def init():
    items = R.load_items()
    item_users = R.load_item_users()
    for asin, meta in items.items():
        meta["rating_number"] = len(item_users.get(asin, ()))
        meta["average_rating"] = None
    G["items"], G["iu"] = items, item_users


def rank_from_scores(scores, order_ids, target):
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    for r, i in enumerate(order, 1):
        if order_ids[i] == target:
            return r
    return len(scores) + 1


def work(lines):
    items, iu = G["items"], G["iu"]
    out = []
    for line in lines:
        case = json.loads(line)
        cands = [R.item_card(a, items) for a in case["candidates"]]
        ids = case["candidates"]
        target = case["ground_truth_item_ids"][0]
        cold = case["slice_labels"]["cold_warm"] == "cold"
        hist = [h["parent_asin"] for h in case["history"]]
        ranks = []
        ranks.append(rank_from_scores(R.b0_scores(cands), ids, target))
        ranks.append(rank_from_scores(R.itemknn_scores(hist, cands, iu), ids, target))
        pop_z = R.zscore([math.log1p(c.get("rating_number") or 0) for c in cands])
        toks = R.tokenize(case["query"])
        if not toks:
            r = rank_from_scores(pop_z, ids, target)
            ranks.extend([r] * len(WEIGHTS))
        else:
            bm = BM25Okapi([R.tokenize(c.get("title") or "") for c in cands])
            bz = R.zscore(list(bm.get_scores(toks)))
            for w in WEIGHTS:
                ranks.append(rank_from_scores([w * b + (1 - w) * p for b, p in zip(bz, pop_z)], ids, target))
        out.append((case["user_key"], cold, ranks))
    return out


def batches(path, limit, size=1500):
    buf = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f):
            if limit is not None and n >= limit:
                break
            buf.append(line)
            if len(buf) == size:
                yield buf
                buf = []
    if buf:
        yield buf


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] != "all" else None
    t0 = time.time()
    users, colds, rks = [], [], []
    with mp.Pool(7, initializer=init) as pool:
        done = 0
        for chunk in pool.imap(work, batches("eval/product_dev_v1.jsonl", limit)):
            for u, c, r in chunk:
                users.append(u)
                colds.append(c)
                rks.append(r)
            done += len(chunk)
            if done % 30000 < 1500:
                print(f"{done:,} cases, {time.time() - t0:.0f}s", flush=True)
    uniq = {u: i for i, u in enumerate(dict.fromkeys(users))}
    np.savez(
        OUT,
        user_idx=np.array([uniq[u] for u in users]),
        cold=np.array(colds),
        ranks=np.array(rks, dtype=np.int16),
        weights=np.array(WEIGHTS),
    )
    print(f"saved {OUT} ({len(users):,} cases, {len(uniq):,} users) in {time.time() - t0:.0f}s", flush=True)
