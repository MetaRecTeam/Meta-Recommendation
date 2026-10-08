# D1 data sheet — Office_Products (Amazon Reviews 2023)

## Source

- **Dataset:** Amazon Reviews 2023, McAuley Lab (UCSD). Official site:
  https://amazon-reviews-2023.github.io/
- **Category chosen:** `Office_Products` — one of 33 categories the dataset
  publishes separately. Chosen as a genuine mid-size category (rank 17 of 33
  by rating count, the literal median), explicitly not Electronics, per the
  recorded decision in the Project Plan (Section 6, "Dataset & split").
- **Why Office_Products specifically:** products in this category carry real
  brand, price and category attributes — the same kind of structure the
  hand-built constraint test cases already use (keyboards, webcams, monitors
  are routinely listed under Office Products on Amazon), so the hard
  constraints (budget/brand/category/model) are meaningfully testable here.
- **Licence / access:** publicly downloadable for research use directly from
  the dataset's own site, no signup or agreement required at the time of
  download (7 Oct 2026).

## Raw files and checksums

Downloaded 6–7 Oct 2026. Kept outside git (`data/` is gitignored); only this
data sheet and the checksums below are tracked in the repository.

| file | size (compressed) | SHA-256 |
|---|---|---|
| `Office_Products.jsonl.gz` (reviews) | 1,616,649,135 bytes (1.51 GB) | `d3551f4e8e15c14e54f8a0dc07b47ec924a54cde161dbe8f24bd457297c6872b` |
| `meta_Office_Products.jsonl.gz` (item metadata) | 528,939,340 bytes (0.49 GB) | `2cd43a529967bc33d2bc074853ed940467e0d592493d0507df56676565d05594` |

Source URLs:
- `https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/review_categories/Office_Products.jsonl.gz`
- `https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/meta_categories/meta_Office_Products.jsonl.gz`

Both files verified against the server's own reported `Content-Length` (exact
byte match) and passed `gzip -t` integrity checks before processing.

## Processing — exact script

Everything below was produced by `scripts/wp4_freeze_dataset.py`, run as:

```bash
python scripts/wp4_freeze_dataset.py --stage extract
python scripts/wp4_freeze_dataset.py --stage filter
python scripts/wp4_freeze_dataset.py --stage split
python scripts/wp4_freeze_dataset.py --stage join
python scripts/wp4_freeze_dataset.py --stage missingness
python scripts/wp4_freeze_dataset.py --stage checksum
```

Memory-safe by design: the machine this ran on had only ~2.4GB free RAM
against a 12.8M-row interaction file, so no stage holds the full interaction
set in memory — counts are small dicts, rows are re-streamed from disk.

### 1. Extract

Pulled `(user_id, parent_asin, timestamp)` from every review record.
`parent_asin` is used as the item identity, not `asin` — the dataset's own
field documentation notes that different colours/styles/sizes of the same
product share one `parent_asin`, while `asin` is the variant-level ID.

- 12,845,712 lines read, 12,845,712 interactions written, 0 malformed.

### 2. 5-core filtering

Iterative: keep only users and items with ≥5 interactions, repeat until
stable (one removal round can push a previously-fine user/item below the
threshold, so this is not a single pass).

- Converged after **13 passes**.
- Final: **1,888,667 interactions**, **233,033 users**, **80,181 items**.
- Independently re-verified after convergence: minimum interaction count
  per user = 5, per item = 5 (recomputed directly from the output file, not
  just trusting the script's own log).

### 3. Chronological split

Split by **time**, not by row count — the last 10% of the *timeline* is the
locked test set, the 10% before that is development, matching the recorded
decision (Project Plan, Section 6).

- Time range: **1999-08-02 to 2023-09-09** (~24 years).
- History/train: 1999-08-02 – 2018-11-13 (first 80% of the span) — 910,722 rows
- Development: 2018-11-13 – 2021-04-12 (next 10%) — 534,128 rows
- **Locked test: 2021-04-12 – 2023-09-09 (last 10%) — 443,817 rows**

Note the row counts are not 80/10/10 — review volume is heavily skewed
toward recent years, so the most recent 10% of the *timeline* contains a
disproportionately large share of the actual interactions (23.5% of rows,
not 10%). This is expected: the split is time-based, not count-based, per
the recorded decision.

### 4. Metadata join

For the 80,181 items that survived 5-core filtering, pulled their product
metadata (title, price, average rating, store, brand, categories) from the
meta file by `parent_asin`.

- 710,503 meta records scanned, **80,181 matched, 0 duplicates, 0 items
  with no metadata at all** — every item that survived filtering has a
  metadata record.

### 5. Missingness report (on the 80,181 joined items)

| field | missing | % |
|---|---|---|
| price | 26,846 | **33.5%** |
| title | 3 | 0.0% |
| average rating | 0 | 0.0% |
| store | 361 | 0.5% |
| brand (from `details.Brand`) | 4,950 | 6.2% |
| categories | 2,858 | 3.6% |

**Notable finding:** price missingness here (33.5%) is roughly 17x higher
than the 2% measured earlier on live SerpApi captures (see
`docs/wp4/D0-interim-report.md`). This is a real difference between this
academic dataset and the live system's data, not noise — worth stating
plainly in the Interim Report. It directly interacts with the recorded
missing-price policy (Project Plan, Section 6, item 11: unknown price is
kept and flagged, excluded only when the query states a budget): on this
dataset, that policy touches roughly a third of the catalogue.

**A correction worth recording, not glossing over:** the first version of
this report counted "missing price" with a naive `price is None` check and
got **26,686 (33.3%)**. Building the actual parser (below) and re-running the
check against it caught **160 additional items** the naive check missed —
their `price` field holds the literal string `"—"` (an em-dash), which is
this dataset's own placeholder for "no price listed," not a JSON `null`. A
null-check alone can't see that; a real parser that tries to extract a
number and fails, can. The corrected, true figure is **26,846 (33.5%)**.
Separately, 43 items hold `"from 8.99"`-style variant-pricing text, which the
parser correctly extracts a real number from — those are genuinely priced,
not missing, and were already (if accidentally) counted correctly before.

## Processed outputs and checksums

All in `data/wp4/processed/` (gitignored; checksums below are the
reproducibility proof, per the proposal's "store checksums and the exact
processing script" requirement).

| file | rows | SHA-256 |
|---|---|---|
| `interactions_5core.tsv` | 1,888,667 | `1c46fc854614d49c5941078a240785d4db79d773f0b5d83434a8ad6b03e9801a` |
| `split/train.tsv` | 910,722 | `9f9d58503e481c2fc273287d48be72999a392d57d54ab4a70c3902172ba866a6` |
| `split/dev.tsv` | 534,128 | `042fb8d487cd1c2556682ff271eab5c65a341a5fd2e19984cce137abc5d4d1ca` |
| `split/test.tsv` | 443,817 | `79eaf5ff4443aed555e9969b59cc8853b7ea377eb9f48a26eb18645753e79c0c` |
| `items_5core.jsonl` | 80,181 | `2b35a60e9ec65a814f678670f7c8d79c1ece6658c875613895413e3ece911304` |

Re-running the full pipeline against the raw files above (same checksums)
reproduces these exact outputs — nothing here depends on live data or
non-deterministic steps.

## ID and price parser

`scripts/wp4_dataset_parser.py`, satisfying the proposal's exact contract
("Prefer ASIN when available ... Prices are a typed (amount, currency) pair
with parser provenance. Missing/failed parse is unknown, never zero.").

- **Identity:** this dataset's own `parent_asin` field already IS the ASIN —
  satisfied "prefer ASIN when available" by construction, no fallback
  identity scheme needed. `parse_item_id()` only validates the format
  (`^[A-Z0-9]{10}$`); all 80,181 items pass. It takes only an ASIN as input —
  there is no title parameter, so merging by title is structurally
  impossible, not just avoided by convention.
- **Price:** `parse_item_price()` reuses the live system's `parse_price()`
  (`product_constraints.py`) rather than reimplementing number parsing, and
  wraps it with the currency (`USD`, the recorded decision) and provenance
  the proposal requires. `ParsedPrice` enforces the "unknown, never zero"
  contract structurally: construction raises `ValueError` if `amount=None`
  carries a currency, or if a real amount carries no currency.
- **9 unit tests**, `tests/test_wp4_dataset_parser.py` — real price, missing
  price, zero-as-a-real-price (distinct from missing), provenance, malformed
  ASIN, empty/None ASIN, and the title-fallback-is-structurally-impossible
  check. All passing.
- **Self-check against the real data:** running the parser across all 80,181
  items and cross-checking its unknown-price count against the measured
  missingness report is what caught the 160-item undercount described above
  — the two now agree exactly (26,846 = 26,846).

## Still open / not yet done

- **ID/identity reconciliation** — this dataset's `parent_asin` and the live
  SerpApi adapter's ASIN-from-link both refer to the same kind of Amazon
  identifier, but no code yet maps between "an item in this frozen dataset"
  and "an item the live system might surface." Whether that mapping is
  needed depends on how D3/D5 end up using this data.
- **Graded relevance judgements for NDCG@10** — the recorded scheme (5★→2,
  4★→1, below→0) is decided but not yet applied to the split files.
