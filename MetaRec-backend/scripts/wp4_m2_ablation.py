"""WP4 ablation: take each component out of M2, re-tune the rest on the tuning users, score on the held-out users.

The report's first ablation (Table "Ablations of the Re-Tuned M2") sets one weight to zero and leaves the other
weights where they were. That can understate what a component is worth, because the remaining weights could
have moved to make up for it. This script does the fairer version: for each component, search every weighting
of the other components (steps of 0.05, summing to 1) on the tuning users, then score the best one on the held-out
users, with the same user-level bootstrap (2,000 draws, seed 42) as the rest of the report.

Inputs are the output of wp4_m2_components.py (component z-scores per candidate) and the D5 per-case artifact (the
B1 ranks and the history length). Nothing here touches the test split and nothing changes M2's live weights.

Run from MetaRec-backend/:
    python scripts/wp4_m2_ablation.py <m2_prefix> artifacts/wp4_d5_dev/per_case.npz artifacts/wp4_ablation
"""
import itertools
import json
import sys
from pathlib import Path

import numpy as np

COMP = ["text_relevance", "attribute_match", "collaborative", "popularity", "tail_adjustment"]
LIVE = np.array([0.25, 0.0, 0.05, 0.65, 0.05])  # the weights the live ranker uses (text, attribute, collab, pop, tail)
CHUNK = 40000
DRAWS = 2000
SEED = 42
CUT_WORDS = 51
TUNING_MOD = 10


def rank_of_target(Z, tgt, w):
    sc = Z @ np.asarray(w, dtype=np.float32)
    ts = sc[np.arange(len(tgt)), tgt]
    idx = np.arange(100)[None, :]
    return 1 + (sc > ts[:, None]).sum(1) + ((sc == ts[:, None]) & (idx < tgt[:, None])).sum(1)


def metric_arrays(r):
    hit = r <= 10
    return hit.astype(float), np.where(hit, 1 / np.log2(r + 1), 0.0), np.where(hit, 1 / r, 0.0)


def simplex(step, k):
    n = round(1 / step)
    for c in itertools.product(range(n + 1), repeat=k - 1):
        if sum(c) <= n:
            yield np.array(list(c) + [n - sum(c)]) * step


def best_weights(Zt, tt, allowed):
    """Best NDCG@10 on the tuning users over every weighting of the `allowed` components."""
    best = (-1.0, None)
    for w_sub in simplex(0.05, len(allowed)):
        w = np.zeros(5)
        w[allowed] = w_sub
        nd = metric_arrays(rank_of_target(Zt, tt, w))[1].mean()
        if nd > best[0]:
            best = (nd, w)
    return best


def main(prefix, per_case_path, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    Z = np.load(f"{prefix}_Z.npy", mmap_mode="r")
    meta = np.load(f"{prefix}_meta.npz")
    tgt, uid = meta["tgt"].astype(np.int64), meta["uid"]
    pc = np.load(per_case_path, allow_pickle=True)
    # the two files must describe the same cases in the same order
    assert len(tgt) == len(pc["uid"]) and (uid == pc["uid"]).all(), "component file and D5 per-case file disagree on users"
    assert (tgt == pc["target_position"].astype(np.int64)).all(), "component file and D5 per-case file disagree on targets"
    systems = [str(s) for s in pc["systems"]]
    b1_rank = pc["ranks"][:, systems.index("b1")].astype(np.int64)
    hist_words = pc["history_words"]

    tune = (uid % TUNING_MOD) == 0
    held = ~tune
    assert (held == pc["held_out"]).all(), "held-out users differ from the D5 report"
    print(f"cases {len(tgt):,}; tuning cases {tune.sum():,}; held-out cases {held.sum():,}", flush=True)

    Zt = np.asarray(Z[np.where(tune)[0]])
    tt = tgt[tune]

    # ---- sanity check: the live weights reproduce the report's tuning-user score (NDCG@10 0.2360, Recall@10 0.3737)
    rc, nd, _ = (a.mean() for a in metric_arrays(rank_of_target(Zt, tt, LIVE)))
    print(f"live weights on the tuning users: Recall@10 {rc:.4f}, NDCG@10 {nd:.4f} (report: 0.3737 / 0.2360)", flush=True)

    variants = {"M2 (live weights)": LIVE.copy()}
    retuned = {}
    for i, c in enumerate(COMP):
        allowed = [j for j in range(5) if j != i]
        nd_best, w = best_weights(Zt, tt, allowed)
        retuned[c] = {"weights": [float(x) for x in w], "tuning_ndcg": float(nd_best)}
        variants[f"re-tuned without {c}"] = w
        print(f"without {c}: best weights {np.round(w, 2).tolist()} (tuning NDCG@10 {nd_best:.4f})", flush=True)
    for i, c in enumerate(COMP):
        w = LIVE.copy()
        w[i] = 0
        if w.sum() > 0:
            variants[f"zeroed {c}, others fixed"] = w

    # ---- score on the held-out users
    held_idx = np.where(held)[0]
    ranks = {k: np.zeros(len(held_idx), dtype=np.int64) for k in variants}
    for s in range(0, len(held_idx), CHUNK):
        sel = held_idx[s:s + CHUNK]
        Zc = np.asarray(Z[sel])
        for k, w in variants.items():
            ranks[k][s:s + CHUNK] = rank_of_target(Zc, tgt[sel], w)
    ranks["B1"] = b1_rank[held]

    hu = uid[held]
    hw = hist_words[held]
    slices = {"all": np.ones(len(hu), dtype=bool), "complex": hw > CUT_WORDS, "keyword": (hw >= 1) & (hw <= CUT_WORDS)}
    users, uinv = np.unique(hu, return_inverse=True)
    U = len(users)
    M = {k: metric_arrays(r) for k, r in ranks.items()}

    def per_user(vals, mask):
        return np.bincount(uinv, weights=vals * mask, minlength=U)

    cnt = {s: per_user(np.ones(len(hu)), m) for s, m in slices.items()}
    num = {(k, s): per_user(M[k][1], m) for k in ranks for s, m in slices.items()}
    point = {(k, s): num[(k, s)].sum() / cnt[s].sum() for k in ranks for s in slices}

    pairs = []
    for k in ranks:
        if k != "B1":
            pairs.append((k, "B1"))
            if k != "M2 (live weights)":
                pairs.append((k, "M2 (live weights)"))
    rng = np.random.default_rng(SEED)
    boots = {(a, b, s): [] for a, b in pairs for s in slices}
    for _ in range(DRAWS):
        w = np.bincount(rng.integers(0, U, U), minlength=U).astype(float)
        den = {s: w @ cnt[s] for s in slices}
        val = {(k, s): (w @ num[(k, s)]) / den[s] if den[s] > 0 else np.nan for k in ranks for s in slices}
        for a, b in pairs:
            for s in slices:
                boots[(a, b, s)].append((val[(a, s)] - val[(b, s)]) / val[(b, s)])

    def lift(a, b, s):
        lo, hi = np.nanpercentile(boots[(a, b, s)], [2.5, 97.5])
        return (point[(a, s)] - point[(b, s)]) / point[(b, s)], lo, hi

    rows = []
    for k in ranks:
        rc, nd, mr = (m.mean() for m in M[k])
        row = {"variant": k, "recall10": float(rc), "ndcg10": float(nd), "mrr10": float(mr)}
        if k in retuned.keys() or k.startswith("re-tuned"):
            row["weights"] = retuned[k.replace("re-tuned without ", "")]["weights"]
        elif k.startswith("zeroed"):
            row["weights"] = [float(x) for x in variants[k]]
        elif k.startswith("M2"):
            row["weights"] = [float(x) for x in LIVE]
        for s in slices:
            row[f"ndcg10_{s}"] = float(point[(k, s)])
            for ref, tag in (("B1", "vs_b1"), ("M2 (live weights)", "vs_m2")):
                if (k, ref, s) in boots:
                    p, lo, hi = lift(k, ref, s)
                    row[f"{tag}_{s}"] = [float(p), float(lo), float(hi)]
        rows.append(row)

    (out / "ablation.json").write_text(json.dumps({"components": COMP, "live_weights": LIVE.tolist(), "retuned": retuned,
                                                  "held_out_cases": int(held.sum()), "held_out_users": int(U),
                                                  "draws": DRAWS, "seed": SEED, "rows": rows}, indent=1), encoding="utf-8")

    def pct(t):
        return f"{t[0] * 100:+.2f}% [{t[1] * 100:+.2f}%, {t[2] * 100:+.2f}%]"

    lines = ["# M2 ablation (held-out users)", "",
             f"{int(held.sum()):,} cases, {U:,} users, {DRAWS:,} user-level bootstrap draws (seed {SEED}). "
             "Weight order: " + ", ".join(COMP) + ".", "",
             "| Variant | Weights | NDCG@10 | vs M2 (all) | vs B1 (all) | vs B1 (complex) |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        w = " / ".join(f"{x:.2f}" for x in r.get("weights", [])) if "weights" in r else "n/a"
        lines.append(f"| {r['variant']} | {w} | {r['ndcg10']:.4f} | {pct(r['vs_m2_all']) if 'vs_m2_all' in r else ''} | "
                     f"{pct(r['vs_b1_all']) if 'vs_b1_all' in r else ''} | {pct(r['vs_b1_complex']) if 'vs_b1_complex' in r else ''} |")
    (out / "ablation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main(*sys.argv[1:4])
