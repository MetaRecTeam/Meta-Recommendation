"""WP4 D5 (light): complex-versus-keyword-like slices by the length of the history text.

The evaluation cases have no real search queries: a case's "query" is the user's last purchases joined
together. As a stand-in for the Plan's complex-query slice, warm users are split by how many words that
history text has: "keyword-like" (short) and "complex" (long). Users with no history are the cold slice.

Two steps, so the cut is fixed before any slice result is seen:
  1. python scripts/wp4_slices.py dist                          -> prints the length distribution only
  2. python scripts/wp4_slices.py run <m2_prefix> <sweep.npz> <cut_in_words>

Step 2 scores held-out users (the 9 in 10 not used to tune M2), reports Recall/NDCG/MRR@10 per slice for
B0 (reviews only), ItemKNN, B1 and M2, and the relative NDCG@10 lift of M2 over B1 per slice with a 95%
user-level bootstrap interval, plus the 2% non-inferiority check. Run from MetaRec-backend/.
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts")
import wp4_run_baselines as R  # noqa: E402
import wp4_m2_tune as T  # noqa: E402

CACHE = Path("eval/history_tokens_dev.npy")
CASES = "eval/product_dev_v1.jsonl"


def history_tokens():
    if CACHE.exists():
        return np.load(CACHE)
    out = []
    with open(CASES, encoding="utf-8") as f:
        for line in f:
            out.append(len(R.tokenize(json.loads(line)["query"])))
    arr = np.array(out, dtype=np.int32)
    np.save(CACHE, arr)
    return arr


def dist():
    h = history_tokens()
    warm = h[h > 0]
    print(f"cases {len(h):,}; cold (no history) {np.mean(h == 0):.4f}; warm {len(warm):,}")
    qs = [10, 25, 33.3, 50, 66.7, 75, 90, 99]
    print("warm history length in words, percentiles:", {q: float(np.percentile(warm, q)) for q in qs})
    print("min", warm.min(), "max", warm.max(), "mean", round(warm.mean(), 1))


def run(prefix, sweep_path, cut):
    h = history_tokens()
    meta = np.load(f"{prefix}_meta.npz")
    uid, tgt, cold = meta["uid"], meta["tgt"].astype(np.int64), meta["cold"]
    sw = np.load(sweep_path)
    W = list(sw["weights"])
    best = np.load(f"{prefix}_best_weights.npy")
    Z = np.load(f"{prefix}_Z.npy", mmap_mode="r")
    held = (uid % 10) != 0
    idx = np.where(held)[0]
    m2 = np.zeros(len(idx), dtype=np.int64)
    for s in range(0, len(idx), T.CHUNK):
        sel = idx[s:s + T.CHUNK]
        m2[s:s + T.CHUNK] = T.rank_of_target(np.asarray(Z[sel]), tgt[sel], best)
    systems = {
        "B0 reviews only": sw["ranks"][idx, 0].astype(np.int64),
        "ItemKNN": sw["ranks"][idx, 1].astype(np.int64),
        "B1": sw["ranks"][idx, 2 + W.index(0.25)].astype(np.int64),
        "M2": m2,
    }
    hh, hu = h[idx], uid[idx]
    slices = {
        "cold (no history)": hh == 0,
        f"keyword-like (1 to {cut} words)": (hh > 0) & (hh <= cut),
        f"complex (more than {cut} words)": hh > cut,
    }
    users_u, uinv = np.unique(hu, return_inverse=True)
    U = len(users_u)
    rng = np.random.default_rng(42)
    draws = [rng.integers(0, U, U) for _ in range(2000)]
    mets = {k: T.metric_arrays(v) for k, v in systems.items()}
    for name, mask in slices.items():
        n_users = len(np.unique(hu[mask]))
        print(f"\n== {name}: {mask.sum():,} cases, {n_users:,} users")
        for k in systems:
            rc, nd, mr = (x[mask].mean() for x in mets[k])
            print(f"  {k:16s} Recall@10 {rc:.4f}  NDCG@10 {nd:.4f}  MRR@10 {mr:.4f}")
        a = np.bincount(uinv, weights=mets["M2"][1] * mask, minlength=U)
        b = np.bincount(uinv, weights=mets["B1"][1] * mask, minlength=U)
        c = np.bincount(uinv, weights=mask.astype(float), minlength=U)
        rel = []
        for d in draws:
            n = c[d].sum()
            if n == 0:
                continue
            rel.append((a[d].sum() - b[d].sum()) / b[d].sum())
        lo, hi = np.percentile(rel, [2.5, 97.5])
        pt = (mets["M2"][1][mask].mean() - mets["B1"][1][mask].mean()) / mets["B1"][1][mask].mean()
        print(f"  M2 over B1, NDCG@10: {pt * 100:+.2f}%  95% interval [{lo * 100:+.2f}%, {hi * 100:+.2f}%]"
              f"  -> non-inferior at the 2% margin: {lo > -0.02}")


if __name__ == "__main__":
    if sys.argv[1] == "dist":
        dist()
    else:
        run(sys.argv[2], sys.argv[3], int(sys.argv[4]))
