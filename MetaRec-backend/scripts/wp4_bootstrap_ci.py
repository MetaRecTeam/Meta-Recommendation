"""WP4 D2: metrics per system, BM25-weight sweep and user-level bootstrap intervals from the ranks saved by wp4_sweep_ranks.py.
Run from MetaRec-backend/:  python scripts/wp4_bootstrap_ci.py sweep_ranks.npz"""
import sys

import numpy as np

d = np.load(sys.argv[1])
ranks = d["ranks"].astype(np.int64)  # cols: b0, itemknn, b1@weights...
users = d["user_idx"]
cold = d["cold"]
W = list(d["weights"])
N, U = len(users), users.max() + 1


def metrics(r):
    hit = r <= 10
    return {
        "recall": hit.astype(float),
        "ndcg": np.where(hit, 1.0 / np.log2(r + 1), 0.0),
        "mrr": np.where(hit, 1.0 / r, 0.0),
    }


systems = {"B0 (reviews only)": ranks[:, 0], "ItemKNN": ranks[:, 1]}
for w in W:
    systems[f"B1 w={w:.2f}"] = ranks[:, 2 + W.index(w)]
M = {k: metrics(v) for k, v in systems.items()}

print(f"cases {N:,}, users {U:,}, cold {cold.mean():.4f}")
print("\n--- all cases / warm / cold (Recall, NDCG, MRR)")
for k, m in M.items():
    row = []
    for sel, nm in ((slice(None), "all"), (~cold, "warm"), (cold, "cold")):
        row.append(f"{nm}: " + " / ".join(f"{m[x][sel].mean():.4f}" for x in ("recall", "ndcg", "mrr")))
    print(f"{k:20s} " + " | ".join(row))

# user-level bootstrap (resample users with replacement; each user keeps all their cases)
cnt = np.bincount(users, minlength=U).astype(float)


def user_sums(x):
    return np.bincount(users, weights=x, minlength=U)


rng = np.random.default_rng(42)
B = 2000
draws = [rng.integers(0, U, U) for _ in range(B)]


def boot(a_key, b_key, metric):
    sa, sb = user_sums(M[a_key][metric]), user_sums(M[b_key][metric])
    abs_d, rel_d, ma, mb = [], [], [], []
    for idx in draws:
        n = cnt[idx].sum()
        a, b = sa[idx].sum() / n, sb[idx].sum() / n
        abs_d.append(a - b)
        rel_d.append((a - b) / b)
        ma.append(a)
        mb.append(b)
    q = lambda v: tuple(np.percentile(v, [2.5, 97.5]))
    point_a, point_b = M[a_key][metric].mean(), M[b_key][metric].mean()
    return point_a, point_b, (point_a - point_b) / point_b, q(rel_d), q(abs_d), q(ma), q(mb)


print("\n--- user-level bootstrap, 2000 resamples, 95% percentile interval")
for a, b in (("B1 w=0.25", "B0 (reviews only)"), ("B1 w=0.25", "ItemKNN"), ("B1 w=0.25", "B1 w=0.00")):
    for metric in ("recall", "ndcg", "mrr"):
        pa, pb, rel, rci, aci, ca, cb = boot(a, b, metric)
        print(f"{a} vs {b} {metric}: {pa:.4f} vs {pb:.4f}  rel lift {rel * 100:+.2f}%  95% CI [{rci[0] * 100:+.2f}%, {rci[1] * 100:+.2f}%]  "
              f"abs diff CI [{aci[0]:+.4f}, {aci[1]:+.4f}]  A CI [{ca[0]:.4f}, {ca[1]:.4f}]  B CI [{cb[0]:.4f}, {cb[1]:.4f}]")
print(f"\nb1 rank<=10 hit but only popularity (w=0) vs w=0.25 per-case agreement: {np.mean(ranks[:, 2 + W.index(0.0)] == ranks[:, 2 + W.index(0.25)]):.4f}")
