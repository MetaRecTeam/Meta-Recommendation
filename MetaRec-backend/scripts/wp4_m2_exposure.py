"""WP4 ablation, exposure side: what each M2 variant shows in its top 10 (held-out users).

Companion to wp4_m2_ablation.py. For the live weights and for every ablation variant that script wrote to
ablation.json, this reports the share of top-10 slots held by head items (12+ train reviews) and the share of the
catalogue that appears in some user's top 10. The live-weights row must reproduce the D5 report (head share 91.5%,
catalogue coverage 70.3%); the script stops if it does not.

The head flag of a candidate is read from the tail_adjustment component (1 for tail, 0 for head, z-scored per case,
so head is the negative value). Candidate identities come from the dev fixture.

Run from MetaRec-backend/:
    python scripts/wp4_m2_exposure.py <m2_prefix> artifacts/wp4_ablation/ablation.json eval/product_dev_v1.jsonl artifacts/wp4_ablation
"""
import json
import sys
from pathlib import Path

import numpy as np

CATALOGUE = 80181
CHUNK = 40000
TUNING_MOD = 10
D5_HEAD_SHARE, D5_COVERAGE = 0.915, 0.703


def load_candidate_ids(fixture, n):
    ids, vocab = np.zeros((n, 100), dtype=np.int32), {}
    with open(fixture, encoding="utf-8") as f:
        for k, line in enumerate(f):
            if k >= n:
                break
            cands = json.loads(line)["candidates"]
            ids[k] = [vocab.setdefault(a, len(vocab)) for a in cands]
    return ids, len(vocab)


def top10(sc):
    return np.argsort(-sc, axis=1, kind="stable")[:, :10]


def main(prefix, ablation_json, fixture, out_dir):
    out = Path(out_dir)
    Z = np.load(f"{prefix}_Z.npy", mmap_mode="r")
    meta = np.load(f"{prefix}_meta.npz")
    uid = meta["uid"]
    held_idx = np.where((uid % TUNING_MOD) != 0)[0]
    ab = json.loads(Path(ablation_json).read_text(encoding="utf-8"))
    variants = {}
    for r in ab["rows"]:
        if "weights" in r:
            variants[r["variant"]] = np.asarray(r["weights"], dtype=np.float32)
    ids, n_items = load_candidate_ids(fixture, len(uid))
    print(f"{len(variants)} variants; {n_items:,} distinct candidate items in the fixture", flush=True)

    head_slots = {k: 0 for k in variants}
    shown = {k: np.zeros(n_items, dtype=bool) for k in variants}
    total_slots = 0
    for s in range(0, len(held_idx), CHUNK):
        sel = held_idx[s:s + CHUNK]
        Zc = np.asarray(Z[sel])
        is_head = Zc[:, :, 4] < 0
        total_slots += 10 * len(sel)
        for k, w in variants.items():
            pos = top10(Zc @ w)
            head_slots[k] += np.take_along_axis(is_head, pos, 1).sum()
            shown[k][np.take_along_axis(ids[sel], pos, 1).ravel()] = True

    rows = []
    for k in variants:
        rows.append({"variant": k, "head_share_top10": float(head_slots[k] / total_slots),
                     "distinct_items": int(shown[k].sum()), "catalogue_coverage": float(shown[k].sum() / CATALOGUE)})
    live = next(r for r in rows if r["variant"].startswith("M2 (live"))
    assert abs(live["head_share_top10"] - D5_HEAD_SHARE) < 0.002 and abs(live["catalogue_coverage"] - D5_COVERAGE) < 0.002, (
        f"live weights do not reproduce the D5 exposure numbers: {live}")
    (out / "exposure.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    lines = ["# M2 ablation: exposure (held-out users, top 10)", "",
             "| Variant | Head share of top 10 | Distinct items shown | Catalogue coverage |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['variant']} | {r['head_share_top10'] * 100:.1f}% | {r['distinct_items']:,} | {r['catalogue_coverage'] * 100:.1f}% |")
    (out / "exposure.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main(*sys.argv[1:5])
