"""WP4 D5 extra: user-level 5-fold cross-validation of M2's weight tuning on the DEV cases.

For each fold k, the weights are tuned on the other four fifths of the users (grid search, same objective as
wp4_m2_tune.py: NDCG@10) and then scored, together with B1, on the held-out fifth. The spread of M2's gain over B1
across the five folds shows whether the gain depends on which users were used to tune.

Folds are by USER (a random permutation of the user ids, seed 42), never by case, so no user is in both the tuning
and the scoring set. Nothing here reads the test split: the input is the dev-case output of wp4_m2_components.py
and wp4_sweep_ranks.py. The sealed test split stays sealed.

Run from MetaRec-backend/:
    python scripts/wp4_m2_cv.py <m2_prefix> <sweep_ranks.npz> <out_dir> [step=0.05] [folds=5]

Each finished fold is saved to <out_dir>/fold<k>.json, so an interrupted run resumes where it stopped.
"""
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts")
import wp4_m2_tune as T  # noqa: E402  (reuses simplex, rank_of_target, metric_arrays)

COMP = T.COMP


def main(prefix, sweep_path, out_dir, step=0.05, folds=5):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    Z = np.load(f"{prefix}_Z.npy", mmap_mode="r")
    meta = np.load(f"{prefix}_meta.npz")
    tgt, uid = meta["tgt"].astype(np.int64), meta["uid"]
    sw = np.load(sweep_path)
    W = list(sw["weights"])
    b1 = sw["ranks"][:, 2 + W.index(0.25)].astype(np.int64)  # B1, BM25 weight 0.25 (as in the report)

    users = np.unique(uid)
    perm = np.random.default_rng(42).permutation(len(users))
    fold_of_user = np.empty(len(users), dtype=np.int64)
    fold_of_user[perm] = np.arange(len(users)) % folds
    fold = fold_of_user[np.searchsorted(users, uid)]
    print(f"cases {len(tgt):,}, users {len(users):,}, {folds} folds, grid step {step}", flush=True)

    weightings = list(T.simplex(step))
    print(f"grid: {len(weightings):,} weightings per fold", flush=True)

    for k in range(folds):
        f = out / f"fold{k}.json"
        if f.exists():
            print(f"fold {k}: already done, skipping", flush=True)
            continue
        t0 = time.time()
        tr = np.where(fold != k)[0]
        te = np.where(fold == k)[0]
        Ztr, ttr = np.asarray(Z[tr]), tgt[tr]
        best_nd, best_w = -1.0, None
        for w in weightings:
            nd = T.metric_arrays(T.rank_of_target(Ztr, ttr, w))[1].mean()
            if nd > best_nd:
                best_nd, best_w = nd, w
        del Ztr
        Zte = np.asarray(Z[te])
        rec_m2, nd_m2, _ = T.metric_arrays(T.rank_of_target(Zte, tgt[te], best_w))
        rec_b1, nd_b1, _ = T.metric_arrays(b1[te])
        res = {
            "fold": k,
            "tuning_cases": int(len(tr)), "tuning_users": int(len(np.unique(uid[tr]))),
            "scored_cases": int(len(te)), "scored_users": int(len(np.unique(uid[te]))),
            "weights": {c: round(float(x), 2) for c, x in zip(COMP, best_w)},
            "tuning_ndcg10": float(best_nd),
            "ndcg10_m2": float(nd_m2.mean()), "ndcg10_b1": float(nd_b1.mean()),
            "recall10_m2": float(rec_m2.mean()), "recall10_b1": float(rec_b1.mean()),
            "gain_over_b1_pct": float((nd_m2.mean() / nd_b1.mean() - 1) * 100),
            "seconds": round(time.time() - t0, 1),
        }
        f.write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(f"fold {k}: M2 {res['ndcg10_m2']:.4f} vs B1 {res['ndcg10_b1']:.4f}  gain {res['gain_over_b1_pct']:+.2f}%  "
              f"weights {list(res['weights'].values())}  ({res['seconds']}s)", flush=True)

    rows = [json.loads((out / f"fold{k}.json").read_text(encoding="utf-8")) for k in range(folds)]
    g = np.array([r["gain_over_b1_pct"] for r in rows])
    summary = {
        "folds": folds, "grid_step": step,
        "gain_over_b1_pct_mean": float(g.mean()), "gain_over_b1_pct_sd": float(g.std(ddof=1)) if folds > 1 else None,
        "gain_over_b1_pct_min": float(g.min()), "gain_over_b1_pct_max": float(g.max()),
        "folds_with_positive_gain": int((g > 0).sum()),
        "per_fold": rows,
    }
    (out / "cv_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    lines = [f"# M2 {folds}-fold user-level cross-validation (dev cases, grid step {step})", "",
             "| Fold | Tuned on (users) | Scored on (users) | NDCG@10 M2 | NDCG@10 B1 | Gain over B1 | Weights " + "/ ".join(COMP) + " |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['fold']} | {r['tuning_users']:,} | {r['scored_users']:,} | {r['ndcg10_m2']:.4f} | "
                     f"{r['ndcg10_b1']:.4f} | {r['gain_over_b1_pct']:+.2f}% | " + " / ".join(f"{v:.2f}" for v in r["weights"].values()) + " |")
    lines += ["", f"Mean gain {summary['gain_over_b1_pct_mean']:+.2f}% (SD {summary['gain_over_b1_pct_sd']:.2f}, "
              f"range {summary['gain_over_b1_pct_min']:+.2f}% to {summary['gain_over_b1_pct_max']:+.2f}%); "
              f"positive in {summary['folds_with_positive_gain']} of {folds} folds."]
    (out / "cv_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))


if __name__ == "__main__":
    a = sys.argv
    main(a[1], a[2], a[3], float(a[4]) if len(a) > 4 else 0.05, int(a[5]) if len(a) > 5 else 5)
