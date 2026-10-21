"""WP4 D3: ranker-only latency (p50 / p95 / max, milliseconds) for B0, B1, ItemKNN and M2.

Times only the call that turns 100 candidate cards plus the user's history into scores; cards are built
beforehand and are not timed. Single process, first N dev cases, leak-safe popularity (train counts).
Run on an otherwise idle machine, from MetaRec-backend/:  python scripts/wp4_latency.py [N]
"""
import json
import platform
import statistics
import sys
import time

sys.path.insert(0, "scripts")
sys.path.insert(0, ".")
import wp4_run_baselines as R  # noqa: E402
from langgraph_metarec import product_ranker_m2 as M  # noqa: E402

W_M2 = {"text_relevance": 0.20, "attribute_match": 0.05, "collaborative": 0.10, "popularity": 0.65, "tail_adjustment": 0.0}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def main(n_cases, m2_weights):
    items = R.load_items()
    iu = R.load_item_users()
    for a, m in items.items():
        m["rating_number"] = len(iu.get(a, ()))
        m["average_rating"] = None
    counts = sorted(m["rating_number"] for m in items.values())
    head = counts[int(len(counts) * 0.80)]
    cases = []
    with open("eval/product_dev_v1.jsonl", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= n_cases:
                break
            cases.append(json.loads(line))
    prepared = []
    for c in cases:
        cands = [R.item_card(a, items) for a in c["candidates"]]
        hist = [h["parent_asin"] for h in c["history"]]
        cats = {items[a]["main_category"] for a in hist if items.get(a, {}).get("main_category")}
        brands = {items[a]["brand"] for a in hist if items.get(a, {}).get("brand")}
        prepared.append((c["query"], cands, hist, cats, brands))

    runners = {
        "B0": lambda q, cands, h, ca, br: R.b0_scores(cands),
        "B1": lambda q, cands, h, ca, br: R.b1_scores(q, cands, 0.25),
        "ItemKNN": lambda q, cands, h, ca, br: R.itemknn_scores(h, cands, iu),
        "M2": lambda q, cands, h, ca, br: M.score_candidates(
            history_asins=h, history_categories=ca, history_brands=br, candidates=cands,
            items=items, item_users=iu, head_threshold=head, weights=m2_weights),
    }
    print(f"{platform.processor()} | python {platform.python_version()} | {len(prepared)} cases | head threshold {head}")
    for name, fn in runners.items():
        for p in prepared[:20]:  # warm-up, not counted
            fn(*p)
        times = []
        for p in prepared:
            t0 = time.perf_counter()
            fn(*p)
            times.append((time.perf_counter() - t0) * 1000)
        print(f"{name:8s} p50 {pct(times, .5):7.2f} ms  p95 {pct(times, .95):7.2f} ms  max {max(times):7.2f} ms  mean {statistics.fmean(times):7.2f} ms")


if __name__ == "__main__":
    w = dict(W_M2)
    if len(sys.argv) > 2:
        w = dict(zip(M.DEFAULT_WEIGHTS.keys(), (float(x) for x in sys.argv[2].split(","))))
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 500, w)
