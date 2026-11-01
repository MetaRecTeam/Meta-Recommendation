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
- **Licence and terms.** Checked on 20 and 21 Oct 2026. The dataset's homepage
  (https://amazon-reviews-2023.github.io/) states no licence and no terms of use; it
  asks users to cite the paper and offers the files for download without a signup. The
  dataset card on Hugging Face (McAuley-Lab/Amazon-Reviews-2023), read on 21 Oct 2026
  after a failed attempt on 20 Oct, has no licence field and no terms section either:
  its metadata header holds only language, tags, size category and the file configs,
  and a search of the whole raw README (30,335 characters) for "licence", "license" and
  "terms" finds nothing. It asks for the paper to be cited. So no licence text for the
  data exists to record. The data is therefore used for academic
  research only: not redistributed, not published, only derived counts and scores are
  reported. To confirm with the supervisor, as Project Plan Section 6 requires for any
  access condition.
- **Citation requested by the dataset.** Hou, Li, He, Yan, Chen, McAuley, "Bridging
  Language and Items for Retrieval and Recommendation", arXiv:2403.03952, 2024.

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

### 6. Star ratings, leak-free item features and graded labels (added 20 Oct 2026)

Two gaps in the first pass were closed without changing any file above (their
checksums below are unchanged).

- **The star rating was not kept.** `extract` wrote only user, item and timestamp. The new
  `ratings` stage re-streams the raw reviews and writes `ratings_5core.tsv`
  (user, item, timestamp, rating): 12,845,712 raw lines read, 1,888,667 rows written, none
  without a rating. Each row is checked against `interactions_5core.tsv` as it is
  written, so the two files line up row for row.
- **The catalogue's `rating_number` and `average_rating` leak.** They count reviews through
  2023, so for a development-period case they include reviews written later, and the
  target review itself. The `leakfree_features` stage computes what was knowable at each
  cutoff instead: `item_features_train.tsv` (per item, interactions and mean star rating
  from train rows only, for scoring dev) and `item_features_train_dev.tsv` (train plus dev,
  for scoring the locked test when it is opened). 32,289 of the 80,181 items (40.3%) have
  no train rows yet, because they were first reviewed after the train cutoff.
- **Graded relevance labels for dev.** `split/dev_graded_relevance.tsv` has one row per dev
  interaction with the rule decided in the Project Plan: 5 stars = 2, 4 stars = 1, below 4
  = 0. Of 534,128 dev rows: 406,502 (76.1%) are 2, 55,753 (10.4%) are 1, 71,873 (13.5%) are 0.
- **What this stage did with the test window.** The ratings file contains the test rows too.
  Rows whose timestamp falls in the test window (443,817) are counted and skipped; their
  ratings are not read into any output.

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
| `ratings_5core.tsv` | 1,888,667 | `4bd9c17684d80710df05aedfc09c18f9c28977a07be10777e1f79e07ddc1b66c` |
| `item_features_train.tsv` | 80,181 | `b70eb55535dfb73bfaad362e7c3ff020d6e7ede794fc1df2d373271d6119b923` |
| `item_features_train_dev.tsv` | 80,181 | `720b15599e4582a4fe84192123df96daa9453a06aaae84ec712b71b647f7c1da` |
| `split/dev_graded_relevance.tsv` | 534,128 | `f41705739dc870f2a4f47c37779106e5f46a0cdac469aeccca9f466d9009ce09` |

Re-running the full pipeline against the raw files above (same checksums)
reproduces these exact outputs — nothing here depends on live data or
non-deterministic steps.

## Evaluation candidate pools (the "provider-style candidates")

The proposal asks for frozen provider-style candidates. There are two sets.

- **Live provider candidates.** The 15 captured SerpApi searches in `tests/wp4/cases/` and
  B0's exact output on them in `tests/wp4/golden/` (frozen in D0).
- **Candidate pools for the Amazon data.** `scripts/wp4_build_eval_cases.py` makes one case
  per development interaction: the real next item plus 99 random items that user never
  touched in train, dev or test, drawn with seed 42. At scoring time each candidate is
  rendered as a provider-style card (title, price, rating, reviews, brand, category) with
  `price_missing`, `price_provenance`, `brand_missing` and `main_category_missing` flags.
  The resulting file `eval/product_dev_v1.jsonl` (534,128 cases, about 1 GB) is not in git.
  Its SHA-256, in `eval/checksums.sha256`, is
  `85a5d9536f57cdfdea6ef6372bd5bb22b9ade011adb52d00ee24610a8c46d92b`. It was rebuilt from
  scratch on 20 Oct 2026 and came out byte-identical.
- **A known limit.** Negatives are drawn from the whole catalogue, which includes the 32,289
  items with no train rows (first reviewed after the train cutoff). A candidate can
  therefore be an item that had no reviews yet at the time of the case.
- No test-split pools have been built; they are made only when the test is opened.

## Data handling

- **Consent and ethics.** No consent was collected, and none could be: the data is a public research dataset of
  reviews released by its authors, and the project recruits and contacts nobody, does not try to identify any
  reviewer and does not use MetaRec user data. No formal ethics determination has been asked for. Whether the
  university needs one for the secondary use of public, pseudonymous data is to be confirmed with the supervisor.
- **Personal data.** The raw files hold pseudonymous reviewer ids supplied by the dataset,
  review text and ratings. No names or contact details. MetaRec user data is not used and
  not published (its interaction table is empty).
- **Minimization.** Of the 10 fields in a review, the processed files keep user id, product
  id, timestamp and rating. Of the 14 catalogue fields, 9 are kept, for the 80,181 items
  that survive the 5-core filter. Review text, images and helpful votes are not copied.
- **Access.** The files sit in `data/wp4/` on the student's laptop and are gitignored. The
  repository holds only the scripts, this sheet and the checksums.
- **Retention.** Kept until the Final Report and oral examination are finished (late
  November 2026). The raw files can be downloaded again and checked against the checksums
  above, so they can be deleted earlier. To confirm with the supervisor.
- **Deletion.** Deleting `data/wp4/raw/`, `data/wp4/processed/` and `eval/*.jsonl` removes
  every copy the project holds; there is no copy elsewhere.
- **Locked-test custody.** `split/test.tsv` (443,817 rows, the last 10% of the timeline;
  SHA-256 `79eaf5ff4443aed555e9969b59cc8853b7ea377eb9f48a26eb18645753e79c0c`) is not used to
  score, tune or inspect any system. It is opened once, at the end, for the final numbers.
  Two reads happen that use no test outcome: building dev cases reads it to exclude a user's
  later items from that user's negatives, and the ratings stages skip its rows. The
  checksum makes any later change detectable.

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

- **ID reconciliation with the live system.** `parent_asin` and the live SerpApi adapter's
  ASIN-from-link are the same kind of identifier, but no code yet maps a frozen-dataset item
  to an item the live system might surface. It depends on how D4 uses this data.
- **The graded labels are not yet used.** `dev_graded_relevance.tsv` exists, but the
  evaluation scripts still report NDCG with binary relevance, and the leak-free item
  features are not yet what the runner reads by default.
- **Licence.** Confirm the licence status with the supervisor (see Source).
