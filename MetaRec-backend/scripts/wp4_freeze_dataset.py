"""WP4 D1 — freeze Office_Products into a 5-core filtered, chronologically split dataset.

Memory-safe by design: the machine this was built on had only ~2.4GB free RAM
against a ~12.8M-row interaction file, so nothing here holds the full
interaction set in memory. Counts are kept as small dicts (user_id -> count,
parent_asin -> count); the actual interaction rows are re-streamed from disk
on every pass.

Stages (run via --stage, in order):
  extract   - pull (user_id, parent_asin, timestamp) out of the raw review
              gzip into a compact intermediate TSV (fast to re-scan).
  filter    - iterative 5-core filtering on the intermediate TSV until no
              more users/items drop below 5 interactions.
  split     - chronological split of the filtered interactions: last 10% of
              the timeline (by time, not by row count) is the locked test
              set, the 10% before that is development, the rest is history.
  checksum  - SHA-256 every processed output file.

Each stage reads the previous stage's output and writes its own; re-run a
single stage without redoing earlier ones.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "wp4" / "raw"
PROC_DIR = Path(__file__).resolve().parents[1] / "data" / "wp4" / "processed"
PROC_DIR.mkdir(parents=True, exist_ok=True)

REVIEW_GZ = RAW_DIR / "Office_Products.jsonl.gz"
META_GZ = RAW_DIR / "meta_Office_Products.jsonl.gz"
INTERACTIONS_TSV = PROC_DIR / "interactions_raw.tsv"
FILTERED_TSV = PROC_DIR / "interactions_5core.tsv"
SPLIT_DIR = PROC_DIR / "split"
ITEMS_JSONL = PROC_DIR / "items_5core.jsonl"
MIN_CORE = 5


def log(msg: str) -> None:
    print(msg, flush=True)


def stage_extract() -> None:
    """Pull (user_id, parent_asin, timestamp) from the raw review gzip."""
    if not REVIEW_GZ.exists():
        sys.exit(f"missing {REVIEW_GZ} — download it first")

    log(f"extracting from {REVIEW_GZ} ...")
    n_in = n_out = n_bad = 0
    with gzip.open(REVIEW_GZ, "rt", encoding="utf-8") as fin, \
         open(INTERACTIONS_TSV, "w", encoding="utf-8") as fout:
        for line in fin:
            n_in += 1
            try:
                obj = json.loads(line)
                user_id = obj["user_id"]
                item_id = obj["parent_asin"]
                ts = int(obj["timestamp"])
            except (KeyError, ValueError, json.JSONDecodeError):
                n_bad += 1
                continue
            fout.write(f"{user_id}\t{item_id}\t{ts}\n")
            n_out += 1
            if n_in % 2_000_000 == 0:
                log(f"  ... {n_in:,} lines read, {n_out:,} written so far")

    log(f"done: {n_in:,} lines read, {n_out:,} interactions written, {n_bad:,} malformed skipped")
    log(f"-> {INTERACTIONS_TSV}")


def _count_pass(path: Path) -> tuple[Counter, Counter, int]:
    user_counts: Counter = Counter()
    item_counts: Counter = Counter()
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            user_id, item_id, _ts = line.rstrip("\n").split("\t")
            user_counts[user_id] += 1
            item_counts[item_id] += 1
            n += 1
    return user_counts, item_counts, n


def stage_filter() -> None:
    """Iterative 5-core filtering, re-streaming from disk each pass (memory-safe)."""
    if not INTERACTIONS_TSV.exists():
        sys.exit(f"missing {INTERACTIONS_TSV} — run --stage extract first")

    current = INTERACTIONS_TSV
    round_no = 0
    while True:
        round_no += 1
        log(f"5-core pass {round_no}: counting ...")
        user_counts, item_counts, n_rows = _count_pass(current)
        bad_users = {u for u, c in user_counts.items() if c < MIN_CORE}
        bad_items = {i for i, c in item_counts.items() if c < MIN_CORE}
        log(f"  pass {round_no}: {n_rows:,} rows, "
            f"{len(user_counts):,} users ({len(bad_users):,} below {MIN_CORE}), "
            f"{len(item_counts):,} items ({len(bad_items):,} below {MIN_CORE})")

        if not bad_users and not bad_items:
            log(f"converged after {round_no} pass(es): every remaining user and item has >= {MIN_CORE}")
            if current != FILTERED_TSV:
                current.replace(FILTERED_TSV)
            break

        next_path = PROC_DIR / f"interactions_5core_round{round_no}.tsv"
        kept = 0
        with open(current, encoding="utf-8") as fin, open(next_path, "w", encoding="utf-8") as fout:
            for line in fin:
                user_id, item_id, ts = line.rstrip("\n").split("\t")
                if user_id in bad_users or item_id in bad_items:
                    continue
                fout.write(line)
                kept += 1
        log(f"  pass {round_no}: kept {kept:,} / {n_rows:,} rows after removing below-threshold users/items")

        if current != INTERACTIONS_TSV:
            current.unlink()  # drop the previous round's intermediate file
        current = next_path

    log(f"-> {FILTERED_TSV}")


def stage_split() -> None:
    """Chronological split: sort by time, last 10% of the TIME RANGE is test,
    the 10% before that is dev, everything earlier is history/train."""
    if not FILTERED_TSV.exists():
        sys.exit(f"missing {FILTERED_TSV} — run --stage filter first")

    log(f"reading timestamps from {FILTERED_TSV} to find the time range ...")
    min_ts = None
    max_ts = None
    n = 0
    with open(FILTERED_TSV, encoding="utf-8") as f:
        for line in f:
            _u, _i, ts = line.rstrip("\n").split("\t")
            ts = int(ts)
            if min_ts is None or ts < min_ts:
                min_ts = ts
            if max_ts is None or ts > max_ts:
                max_ts = ts
            n += 1

    assert min_ts is not None and max_ts is not None
    span = max_ts - min_ts
    dev_cutoff = min_ts + int(span * 0.80)
    test_cutoff = min_ts + int(span * 0.90)

    import datetime as dt
    def human(ms: int) -> str:
        return dt.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")

    log(f"{n:,} interactions, time range {human(min_ts)} .. {human(max_ts)}")
    log(f"history/train : {human(min_ts)} .. {human(dev_cutoff)}  (first 80% of the span)")
    log(f"development   : {human(dev_cutoff)} .. {human(test_cutoff)}  (next 10%)")
    log(f"locked test   : {human(test_cutoff)} .. {human(max_ts)}  (last 10%)")

    SPLIT_DIR.mkdir(parents=True, exist_ok=True)
    counts = {"train": 0, "dev": 0, "test": 0}
    files = {
        "train": open(SPLIT_DIR / "train.tsv", "w", encoding="utf-8"),
        "dev": open(SPLIT_DIR / "dev.tsv", "w", encoding="utf-8"),
        "test": open(SPLIT_DIR / "test.tsv", "w", encoding="utf-8"),
    }
    try:
        with open(FILTERED_TSV, encoding="utf-8") as f:
            for line in f:
                _u, _i, ts = line.rstrip("\n").split("\t")
                ts_int = int(ts)
                if ts_int < dev_cutoff:
                    bucket = "train"
                elif ts_int < test_cutoff:
                    bucket = "dev"
                else:
                    bucket = "test"
                files[bucket].write(line)
                counts[bucket] += 1
    finally:
        for fh in files.values():
            fh.close()

    log(f"split written: train={counts['train']:,} dev={counts['dev']:,} test={counts['test']:,}")
    meta = {
        "min_timestamp_ms": min_ts,
        "max_timestamp_ms": max_ts,
        "min_date": human(min_ts),
        "max_date": human(max_ts),
        "dev_cutoff_ms": dev_cutoff,
        "dev_cutoff_date": human(dev_cutoff),
        "test_cutoff_ms": test_cutoff,
        "test_cutoff_date": human(test_cutoff),
        "counts": counts,
        "total_interactions": n,
    }
    with open(SPLIT_DIR / "split_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    log(f"-> {SPLIT_DIR}/ (train.tsv, dev.tsv, test.tsv, split_meta.json)")


def stage_join() -> None:
    """Pull product metadata (title, price, store, categories, brand) for
    just the items that survived 5-core filtering — not the full 710K-item
    catalog, only the 80K-ish we actually need."""
    if not FILTERED_TSV.exists():
        sys.exit(f"missing {FILTERED_TSV} — run --stage filter first")
    if not META_GZ.exists():
        sys.exit(f"missing {META_GZ} — download it first")

    log("collecting the set of item ids we actually need ...")
    needed: set[str] = set()
    with open(FILTERED_TSV, encoding="utf-8") as f:
        for line in f:
            _u, item_id, _ts = line.rstrip("\n").split("\t")
            needed.add(item_id)
    log(f"  {len(needed):,} distinct items to look up")

    log(f"scanning {META_GZ} for matching records ...")
    n_scanned = n_matched = n_dupe = 0
    seen: set[str] = set()
    with gzip.open(META_GZ, "rt", encoding="utf-8") as fin, \
         open(ITEMS_JSONL, "w", encoding="utf-8") as fout:
        for line in fin:
            n_scanned += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_id = obj.get("parent_asin")
            if item_id not in needed:
                continue
            if item_id in seen:
                n_dupe += 1
                continue
            seen.add(item_id)
            brand = None
            details = obj.get("details")
            if isinstance(details, dict):
                brand = details.get("Brand")
            record = {
                "parent_asin": item_id,
                "title": obj.get("title"),
                "price": obj.get("price"),
                "average_rating": obj.get("average_rating"),
                "rating_number": obj.get("rating_number"),
                "store": obj.get("store"),
                "brand": brand,
                "categories": obj.get("categories") or [],
                "main_category": obj.get("main_category"),
            }
            fout.write(json.dumps(record, ensure_ascii=False) + "\n")
            n_matched += 1
            if n_scanned % 200_000 == 0:
                log(f"  ... {n_scanned:,} meta records scanned, {n_matched:,} matched so far")

    missing = needed - seen
    log(f"done: {n_scanned:,} meta records scanned, {n_matched:,} matched, "
        f"{n_dupe:,} duplicate parent_asin skipped, {len(missing):,} items have NO metadata at all")
    if missing:
        sample = list(missing)[:5]
        log(f"  sample of items with no metadata record: {sample}")
    log(f"-> {ITEMS_JSONL}")


def stage_missingness() -> None:
    """Measured field-presence report on the joined item metadata — same
    spirit as the earlier SerpApi audit, but for this frozen dataset."""
    if not ITEMS_JSONL.exists():
        sys.exit(f"missing {ITEMS_JSONL} — run --stage join first")

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from wp4_dataset_parser import parse_item_price  # noqa: E402  (local import: avoid a hard dep for other stages)

    n = 0
    missing_price_naive = missing_price = missing_title = missing_rating = 0
    missing_store = missing_brand = missing_categories = 0
    with open(ITEMS_JSONL, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            n += 1
            # Naive check (what the first version of this report used): only
            # counts a literal JSON null. Kept for transparency about the fix.
            if obj.get("price") is None:
                missing_price_naive += 1
            # Real check: run the actual parser. Catches placeholder strings
            # like "—" that are present-but-unparseable, not just nulls.
            if parse_item_price(obj.get("price")).amount is None:
                missing_price += 1
            if not obj.get("title"):
                missing_title += 1
            if obj.get("average_rating") is None:
                missing_rating += 1
            if not obj.get("store"):
                missing_store += 1
            if not obj.get("brand"):
                missing_brand += 1
            if not obj.get("categories"):
                missing_categories += 1

    def pct(x: int) -> str:
        return f"{x:,} ({x / n:.1%})"

    report = {
        "total_items": n,
        "missing_price": missing_price,
        "missing_price_pct": round(missing_price / n, 4),
        "missing_price_naive_null_check": missing_price_naive,
        "missing_price_naive_vs_real_gap": missing_price - missing_price_naive,
        "missing_title": missing_title,
        "missing_average_rating": missing_rating,
        "missing_store": missing_store,
        "missing_brand_field": missing_brand,
        "missing_categories": missing_categories,
    }
    log(f"total items: {n:,}")
    log(f"missing price (real parser): {pct(missing_price)}")
    log(f"  (naive null-only check would have said: {pct(missing_price_naive)} "
        f"-- missed {missing_price - missing_price_naive} placeholder-string prices)")
    log(f"missing title:      {pct(missing_title)}")
    log(f"missing avg rating: {pct(missing_rating)}")
    log(f"missing store:      {pct(missing_store)}")
    log(f"missing brand:      {pct(missing_brand)}")
    log(f"missing categories: {pct(missing_categories)}")

    out_path = PROC_DIR / "missingness_report.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    log(f"-> {out_path}")


def stage_checksum() -> None:
    """SHA-256 every processed output, appended to processed/checksums.sha256."""
    targets = [
        FILTERED_TSV, SPLIT_DIR / "train.tsv", SPLIT_DIR / "dev.tsv", SPLIT_DIR / "test.tsv",
        ITEMS_JSONL,
    ]
    out_path = PROC_DIR / "checksums.sha256"
    with open(out_path, "w", encoding="utf-8") as out:
        for path in targets:
            if not path.exists():
                log(f"SKIP (missing): {path}")
                continue
            h = hashlib.sha256()
            with open(path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    h.update(chunk)
            digest = h.hexdigest()
            out.write(f"{digest}  {path.name}\n")
            log(f"{digest}  {path.name}")
    log(f"-> {out_path}")


STAGES = {
    "extract": stage_extract,
    "filter": stage_filter,
    "split": stage_split,
    "join": stage_join,
    "missingness": stage_missingness,
    "checksum": stage_checksum,
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=list(STAGES) + ["all"], required=True)
    args = parser.parse_args()

    if args.stage == "all":
        for name, fn in STAGES.items():
            log(f"=== stage: {name} ===")
            fn()
    else:
        STAGES[args.stage]()
