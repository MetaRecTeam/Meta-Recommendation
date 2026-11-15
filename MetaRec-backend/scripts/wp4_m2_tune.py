"""WP4 D3: tune M2's weights on a slice of users, then score the other users.

Input is the output of wp4_m2_components.py (component z-scores per candidate) and, for the comparison rows,
the ranks saved by wp4_sweep_ranks.py. Users are split by a hash of their id: 1 in 10 users tune the weights,
the other 9 in 10 are only ever scored. Nothing here touches the test split.

Run from MetaRec-backend/:
    python scripts/wp4_m2_tune.py <m2_prefix> <sweep_ranks.npz>
"""
import itertools
import sys

import numpy as np

COMP = ["text_relevance", "attribute_match", "collaborative", "popularity", "tail_adjustment"]
CHUNK = 40000


def rank_of_target(Z, tgt, w):
    sc = Z @ np.asarray(w, dtype=np.float32)
    ts = sc[np.arange(len(tgt)), tgt]
    idx = np.arange(100)[None, :]
    return 1 + (sc > ts[:, None]).sum(1) + ((sc == ts[:, None]) & (idx < tgt[:, None])).sum(1)


def metric_arrays(r):
    hit = r <= 10
    return hit.astype(float), np.where(hit, 1 / np.log2(r + 1), 0.0), np.where(hit, 1 / r, 0.0)


def simplex(step=0.05, k=5):
    n = round(1 / step)
    for c in itertools.product(range(n + 1), repeat=k - 1):
        if sum(c) <= n:
            yield np.array(list(c) + [n - sum(c)]) * step


def main(prefix, sweep_path):
    Z = np.load(f"{prefix}_Z.npy", mmap_mode="r")
    meta = np.load(f"{prefix}_meta.npz")
    tgt, uid, cold, grade = meta["tgt"].astype(np.int64), meta["uid"], meta["cold"], meta["grade"]
    b0_legacy = meta["b0_legacy_rank"].astype(np.int64)
    sw = np.load(sweep_path)
    W = list(sw["weights"])
    base = {
        "B0 legacy order, train-only ratings": b0_legacy,
        "B0 reviews only": sw["ranks"][:, 0].astype(np.int64),
        "ItemKNN": sw["ranks"][:, 1].astype(np.int64),
        "B1 (BM25 weight 0.25)": sw["ranks"][:, 2 + W.index(0.25)].astype(np.int64),
    }
    N = len(tgt)
    tune = (uid % 10) == 0
    held = ~tune
    print(f"cases {N:,}; tuning users' cases {tune.sum():,} ({len(np.unique(uid[tune])):,} users); "
          f"held-out cases {held.sum():,} ({len(np.unique(uid[held])):,} users)")

    # ---- grid search on the tuning slice
    Zt = np.asarray(Z[np.where(tune)[0]])
    tt = tgt[tune]
    grid = []
    for w in simplex(0.05):
        r = rank_of_target(Zt, tt, w)
        grid.append((metric_arrays(r)[1].mean(), metric_arrays(r)[0].mean(), w))
    grid.sort(key=lambda x: -x[0])
    print(f"\n--- grid search: {len(grid):,} weightings (step 0.05, sum 1), score = NDCG@10 on tuning slice")
    print("rank | NDCG@10 | Recall@10 | " + " | ".join(COMP))
    for i, (nd, rc, w) in enumerate(grid[:10], 1):
        print(f"{i} | {nd:.4f} | {rc:.4f} | " + " | ".join(f"{x:.2f}" for x in w))
    best = grid[0][2]
    prev = np.array([0.20, 0.05, 0.10, 0.65, 0.0])
    default = np.array([0.25, 0.10, 0.15, 0.45, 0.05])
    # B1 equivalent inside M2 terms: text 0.25, popularity 0.75 (title-only text is not stored, so this is enriched text)
    configs = {
        "M2 tuned (this run)": best,
        "M2 previous best (tuned on leaking popularity)": prev,
        "M2 starting weights": default,
        "M2 text 0.25 + popularity 0.75": np.array([0.25, 0, 0, 0.75, 0]),
    }
    for i, c in enumerate(COMP):
        if best[i] > 0:
            w = best.copy()
            w[i] = 0
            configs[f"tuned, without {c}"] = w
    for i, c in enumerate(COMP):
        w = np.zeros(5)
        w[i] = 1
        configs[f"only {c}"] = w

    # ---- score every config on the held-out users, chunked over the memmap
    held_idx = np.where(held)[0]
    ranks = {k: np.zeros(len(held_idx), dtype=np.int64) for k in configs}
    for s in range(0, len(held_idx), CHUNK):
        sel = held_idx[s:s + CHUNK]
        Zc = np.asarray(Z[sel])
        for k, w in configs.items():
            ranks[k][s:s + CHUNK] = rank_of_target(Zc, tgt[sel], w)
    table = {k: v[held] for k, v in base.items()}
    table.update(ranks)
    hold_uid = uid[held]
    hold_cold = cold[held]
    hold_grade = grade[held]
    users_u, uinv = np.unique(hold_uid, return_inverse=True)
    U = len(users_u)
    cnt = np.bincount(uinv, minlength=U).astype(float)

    print("\n--- held-out users: Recall@10 / NDCG@10 / MRR@10 | graded NDCG@10 | warm NDCG@10 | cold NDCG@10")
    M = {}
    for k, r in table.items():
        rc, nd, mr = metric_arrays(r)
        M[k] = (rc, nd, mr)
        gnd = nd * (hold_grade > 0)
        print(f"{k:48s} {rc.mean():.4f} / {nd.mean():.4f} / {mr.mean():.4f} | {gnd.mean():.4f} | "
              f"{nd[~hold_cold].mean():.4f} | {nd[hold_cold].mean():.4f}")

    # ---- user-level bootstrap
    rng = np.random.default_rng(42)
    draws = [rng.integers(0, U, U) for _ in range(2000)]

    def boot(a, b, j):
        sa = np.bincount(uinv, weights=M[a][j], minlength=U)
        sb = np.bincount(uinv, weights=M[b][j], minlength=U)
        rel = []
        for idx in draws:
            n = cnt[idx].sum()
            x, y = sa[idx].sum() / n, sb[idx].sum() / n
            rel.append((x - y) / y)
        pa, pb = M[a][j].mean(), M[b][j].mean()
        lo, hi = np.percentile(rel, [2.5, 97.5])
        return (pa - pb) / pb, lo, hi

    print("\n--- user-level bootstrap on held-out users (2000 resamples, 95% interval), relative lift")
    pairs = [("M2 tuned (this run)", "B1 (BM25 weight 0.25)"),
             ("M2 tuned (this run)", "B0 reviews only"),
             ("M2 tuned (this run)", "ItemKNN"),
             ("M2 previous best (tuned on leaking popularity)", "B1 (BM25 weight 0.25)")]
    for a, b in pairs:
        out = []
        for j, nm in enumerate(("Recall", "NDCG", "MRR")):
            rel, lo, hi = boot(a, b, j)
            out.append(f"{nm} {rel * 100:+.2f}% [{lo * 100:+.2f}%, {hi * 100:+.2f}%]")
        print(f"{a} vs {b}: " + " | ".join(out))
    for k in table:
        if k.startswith("tuned, without") or k.startswith("only "):
            rel, lo, hi = boot(k, "M2 tuned (this run)", 1)
            print(f"ablation {k} vs tuned M2: NDCG {rel * 100:+.2f}% [{lo * 100:+.2f}%, {hi * 100:+.2f}%]")
    np.save(f"{prefix}_best_weights.npy", best)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
