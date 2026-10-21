"""WP4 D3: one pass over the dev cases that saves M2's five component z-scores for every candidate.

Popularity is the train-only count (leak-safe). Also saves, per case: the position of the real next item in
the candidate list, a user id for resampling, the cold flag, the graded label (5 stars = 2, 4 = 1, else 0) and
the rank of the real item under a leak-free legacy B0 (train mean rating, then train review count, then title).

Weights are applied later by wp4_m2_tune.py, so any weighting can be scored without another pass.

Run from MetaRec-backend/:  python scripts/wp4_m2_components.py [limit|all] [out_prefix]
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
from langgraph_metarec import product_ranker_m2 as M  # noqa: E402

COMPONENTS = ["text_relevance", "attribute_match", "collaborative", "popularity", "tail_adjustment"]
G = {}


def init():
    items = R.load_items()
    item_users = R.load_item_users()
    mean_rating = {}
    with open(R.PROC_DIR / "item_features_train.tsv", encoding="utf-8") as f:
        for line in f:
            asin, _n, mean = line.rstrip("\n").split("\t")
            mean_rating[asin] = float(mean) if mean else None
    for asin, meta in items.items():
        meta["rating_number"] = len(item_users.get(asin, ()))
        meta["average_rating"] = mean_rating.get(asin)
    counts = sorted(m["rating_number"] for m in items.values())
    G.update(items=items, iu=item_users, head=counts[int(len(counts) * 0.80)])


def work(args):
    start, lines = args
    items, iu, head = G["items"], G["iu"], G["head"]
    n = len(lines)
    Z = np.zeros((n, 100, 5), dtype=np.float32)
    tgt = np.zeros(n, dtype=np.int16)
    uid = np.zeros(n, dtype=np.int64)
    cold = np.zeros(n, dtype=bool)
    b0r = np.zeros(n, dtype=np.int16)
    for k, line in enumerate(lines):
        case = json.loads(line)
        cands = [R.item_card(a, items) for a in case["candidates"]]
        target = case["ground_truth_item_ids"][0]
        hist = [h["parent_asin"] for h in case["history"]]
        cats = {items[a]["main_category"] for a in hist if items.get(a, {}).get("main_category")}
        brands = {items[a]["brand"] for a in hist if items.get(a, {}).get("brand")}
        Z[k, :, 0] = M.text_relevance_scores(hist, cands, items)
        Z[k, :, 1] = M.attribute_match_scores(cats, brands, cands)
        Z[k, :, 2] = M.collaborative_scores(hist, cands, iu)
        Z[k, :, 3] = M.popularity_scores(cands)
        Z[k, :, 4] = M.tail_adjustment_scores(cands, head)
        tgt[k] = case["candidates"].index(target)
        uid[k] = int(case["user_key"], 16) % (2**62)
        cold[k] = case["slice_labels"]["cold_warm"] == "cold"
        b0r[k] = R.rank_of_target(R.b0_scores(cands), cands, target)
    return start, Z, tgt, uid, cold, b0r


def batches(path, limit, size=1000):
    buf, start = [], 0
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


def graded_labels(limit):
    out = []
    with open(R.SPLIT_DIR / "dev_graded_relevance.tsv", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if limit is not None and i >= limit:
                break
            out.append(int(line.rstrip("\n").split("\t")[4]))
    return np.array(out, dtype=np.int8)


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] != "all" else None
    prefix = sys.argv[2] if len(sys.argv) > 2 else "m2_components"
    total = limit if limit else sum(1 for _ in open("eval/product_dev_v1.jsonl", encoding="utf-8"))
    Zmm = np.lib.format.open_memmap(f"{prefix}_Z.npy", mode="w+", dtype=np.float32, shape=(total, 100, 5))
    tgt = np.zeros(total, dtype=np.int16)
    uid = np.zeros(total, dtype=np.int64)
    cold = np.zeros(total, dtype=bool)
    b0r = np.zeros(total, dtype=np.int16)
    t0 = time.time()
    done = 0
    with mp.Pool(7, initializer=init) as pool:
        for start, Z, t, u, c, b in pool.imap(work, batches("eval/product_dev_v1.jsonl", limit)):
            end = start + len(t)
            Zmm[start:end], tgt[start:end], uid[start:end], cold[start:end], b0r[start:end] = Z, t, u, c, b
            done += len(t)
            if done % 20000 < 1000:
                print(f"{done:,} cases, {time.time() - t0:.0f}s", flush=True)
    Zmm.flush()
    np.savez(f"{prefix}_meta.npz", tgt=tgt, uid=uid, cold=cold, b0_legacy_rank=b0r, grade=graded_labels(limit))
    print(f"saved {prefix}_* ({total:,} cases) in {time.time() - t0:.0f}s", flush=True)
