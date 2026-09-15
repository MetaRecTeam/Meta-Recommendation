# FYP Project Log — Ong Chuan Hau — WP4: Constraint-aware Product Recommendation

Proposal: https://github.com/Hanny658/FYP26-Proposals/tree/main/WP4-product-recommendation
D0 interim report: [`D0-interim-report.md`](D0-interim-report.md) — pipeline trace, B0 repro, ID/price/field/feedback audit, freeze list.

This file is the running work log; `D0-interim-report.md` is the deliverable.

**Models:** B0 = existing generic order (`_item_score`). B1 = classical BM25 +
calibrated popularity (+ CF) — *not started*. M2 = constraint-aware hybrid: hard
eligibility gate → relevance + user affinity + calibrated popularity / head-tail.
Selected at runtime by `METAREC_PRODUCT_RANKER` (accepts `legacy|domain_v1`, default `legacy`).

**Deliverables:** D0 audit + B0 repro + constraint/price analysis · D1 frozen
dataset + data sheet + parser + missingness report · D2 B0/B1 tuning · D3 M2
ranker (constraints + hybrid scoring + fallback + explanations) · D4 integration
+ flag + strict projection · D5 one-command eval (Recall@10 / NDCG@10 / MRR@10,
latency p50/p95, per-slice).

**Acceptance:** (1) feature-off == B0 fixtures, offline; (2) zero hard-constraint
violations on the labelled corpus; (3) M2 NDCG@10 > B0/B1 (≥5% rel. lift);
(4) complex-query slice improves, head/tail reported; (5) p95 ≤ 25 ms / 100
candidates, always a valid fallback.

**Progress vs plan (S1):** W1–4 audit → done for the constraint/price/ID axes
(this log). W5–8 B0/B1 → B0 reproduced, B1 not started. W9–13 M2 → the hard
eligibility gate (D3, part 1) is built early and behind the flag; hybrid scoring,
affinity, head/tail, and the NDCG/latency harness remain.

## Baseline

- **Pinned commit (B0):** `9c093469eb1a9bbf3fb43bff677994ce8facd70e`
- Branch cut from: `main` (== `origin/main` HEAD)
- Date pinned: 28 Aug 2026
- Commit message: "Merge pull request #16 from Im-jn/feat/itinerary — Itinerary Planning mode with NeSy solver"
- Working branch: `feat/wp4-constraint-aware-product`

---

## B0 Baseline Failure Case #1 — 28 Aug 2026

Query: "recommend me a quiet mechanical keyboard under $120 for Mac"
Follow-up brand selection: Corsair
System: current MetaRec baseline (B0), commit `9c093469eb1a9bbf3fb43bff677994ce8facd70e`
LLM: openai/gpt-oss-20b via Groq

Constraints stated by query: budget <= $120, category = mechanical keyboard,
Mac-compatible, quiet.

Results returned: 9 items total.

**VIOLATIONS FOUND:**
1. Corsair K65 Plus Wireless RGB 75% — $129.99 — exceeds $120 budget (8.3% over)
2. Corsair K100 AIR Wireless RGB — $186.03 — exceeds $120 budget (55% over)
3. Corsair K55 CORE RGB — listed as "Membrane Wired Gaming Keyboard" —
   violates "mechanical" category constraint despite mechanical being
   explicitly requested

Non-violating items (for contrast): Corsair K70 CORE TKL ($89.99),
Corsair K70 CORE with Palmrest ($109.99), Corsair K65 Plus Renewed ($69.99)
— all within budget and correctly mechanical.

**RELEVANCE TO WP4:** direct, first-hand evidence of the exact failure
mode the proposal's acceptance criteria addresses — "zero hard-constraint
violations... never show something over budget." Current B0 ranker
(`_item_score` in `generic_graph.py`) sorts only by (rating, reviews_count,
popularity, title) with no constraint filtering at all, which explains why
category and price constraints are silently ignored.

**NEXT STEP:** use this exact query + result set as a labeled test case when
building the hard-constraint filter (Phase 1 of M2).

---

## Phase 0 — baseline leak harness — 30 Aug 2026

Built `MetaRec-backend/tests/wp4/` — a labeled-dataset harness that feeds a
hand-labeled candidate pool through the real graph
(`run_generic_domain_graph`, fake `amazon.product.search` adapter) and counts
how many hard-constraint-violating items survive to `result.items`.

- `constraints.py` — `HardConstraints` model + `violations(item, hc)` checker
  (price parse, category include/exclude, brand in/not-in, require/exclude
  terms). Reused as the Phase 1 filter core.
- `dataset.py` / `cases/*.json` — one case per query; each candidate carries
  `expect_violations` (human ground truth).
- `eval_constraints.py` — `python -m tests.wp4.eval_constraints` prints the
  leak report. `test_wp4_baseline.py` pins it: `test_harness_runs` (always on)
  + `test_no_hard_constraint_violations_surface` (`xfail(strict)`, the WP4
  acceptance target — flips to pass when Phase 1 lands).

`capture.py` — `python -m tests.wp4.capture "<query>" --pref k=v --out cases/X.json`
runs the query against the **live** SerpApi adapter and writes a case skeleton;
fill `hard_constraints` and label every `expect_violations` by hand.

**B0 numbers — 15 cases (12 with violators + 3 clean controls), 31 Aug 2026:**

| case | kind | pool violators | leaked | first violator rank |
|---|---|---|---|---|
| case_01_keyboard | transcribed, budget + category | 3 | 3 | 3 — top 3 |
| case_02_keyboard_live | budget + category | 3 | 3 | 4 |
| case_03_mouse | clean control (loose budget) | 0 | — | — |
| case_04_headphones | clean control (loose budget) | 0 | — | — |
| case_05_sony_headphones | brand + budget + wired | 3 | 3 | 6 |
| case_06_monitor | tight budget | 7 | 7 | 1 — top 3 |
| case_07_earbuds | brand-exclude + wireless + budget | 2 | 2 | 1 — top 3 |
| case_08_keyboard_tight | clean control ("tight" ≠ below floor) | 0 | — | — |
| case_09_ssd | budget + category — **all 10 violate** | 10 | 10 | 1 — top 3 |
| case_10_standing_desk | tight budget | 6 | 6 | 2 — top 3 |
| case_11_speaker | compound: budget + attribute | 3 | 3 | 7 |
| case_12_webcam | brand-include + budget | 2 | 2 | 2 — top 3 |
| case_13_gaming_laptop | budget + GPU — **all 10 violate** | 10 | 10 | 1 — top 3 |
| case_14_espresso | budget | 2 | 2 | 1 — top 3 |
| case_15_studio_headphones | budget (1 over) | 1 | 1 | 1 — top 3 |
| **TOTAL** | | **52** | **52 (100%)** | — |

**B0 leak rate on labelled hard-constraint violators: 100% (52/52).** There is
no filter anywhere, so every violating item present in the candidate pool is
surfaced. **9 of 15 cases put a violator in the top 3**; 6 put one at rank 1.
Cases 09 and 13 have no compliant candidate at all — Phase 1 must relax or say
"nothing matched", not silently show violations.

`_item_score` sorts by (rating, reviews, popularity, title), so a well-reviewed
over-budget item (K100 AIR $186; Logitech MX Mechanical $153 / 2.2k reviews;
Sony WH-1000XM5 $200) outranks compliant cheaper options. No filter anywhere =
**every violator present in the pool is surfaced.**

**Finding — when B0 leaks:** a *loose* single budget on a category with many
cheap options (mouse < $50, headphones < $100) returns 10 compliant items on
SerpApi relevance alone — 0 leak. Leak needs a **tight budget vs the category's
price band**, a **brand** filter, a **category Amazon's text search ignores**,
or a **compound** constraint. The dataset needs a majority of these.

**Finding — brand is unreliable:** SerpApi returns `brand` for some amazon
results and `null` for others in the same response. `constraints._brand()` only
trusts a real brand string (never the price-filled `subtitle`); `brand_in` is a
violation only when a *different* brand is positively readable. Hand-label brand
cases.

**Findings for Phase 1:**
- `_amazon_product_search_adapter` (tool_registry.py) captures only
  `title, brand, link, rating, reviews, price, thumbnail` — **no category
  field**. Category constraints can only be enforced from the title today;
  either add a category/`type` passthrough or accept title-substring matching.
- SerpApi amazon frequently returns `brand: null`; don't rely on it for the
  brand constraint — fall back to title.
- `price` is a display string (`"$129.99"`, sometimes a `"$x - $y"` range) —
  needs parsing; there is no `extracted_price` passthrough. ~1 in 10 results
  has **no price at all** (cases 05, 11) — policy decision needed: hide, or
  show flagged as "price unavailable".
- The current product `PreferenceSpec` (preference_specs.py) is 6 free-text
  fields. Phase 1 needs structured extraction: `budget_range {max, currency}`,
  `category`, `brand_in` / `brand_not_in`, `required_attributes`.
- Filter belongs in `generic_graph.normalize_and_rank`, right before
  `state["items"] = _rank_items(items)[:10]` (~line 899). `constraints.violations()`
  is the checker; keep the pre-filter candidate list so the relax path can fall
  back to it.

## Phase 0 — DONE

15 cases (12 leaky + 3 clean controls), harness + acceptance tests committed.
B0 baseline = 100% leak (52/52). `python -m tests.wp4.eval_constraints --baseline`
reproduces it.

---

## Phase 1 — hard-constraint filter — 31 Aug 2026

New module `langgraph_metarec/product_constraints.py` (Phase 0's
`tests/wp4/constraints.py` moved here and extended):

- `ProductConstraints` + `violations(item, c)` — the checker.
- `resolve_constraints(query, preferences)` — explicit
  `preferences["hard_constraints"]` dict wins (extraction step / tests);
  otherwise `derive_constraints()` builds a conservative set from the loose
  fields + a small attribute vocabulary in the query text.
- `apply_hard_constraints(items, c)` → `FilterOutcome{kept, dropped, relaxed,
  exhausted}`. Drops violators; if nothing survives, relaxes the non-budget
  constraints one rung at a time (`attributes → exclusions → category →
  brand`); **budget is never auto-relaxed** — an all-over-budget pool returns
  empty + `exhausted`.

Wired into `generic_graph.normalize_and_rank`: **only when
`METAREC_PRODUCT_RANKER` != `legacy`** (acceptance criterion 1 — feature-off is
byte-identical to B0, proven by `test_feature_off_matches_b0_ordering`). Filters
the ranked candidates before the top-10 cut; records the outcome in
`metadata["constraint_filter"]`; appends an "explained empty" error when
exhausted.

**Results — `python -m tests.wp4.eval_constraints`:**

| | B0 (no filter) | B1 (filter on) |
|---|---|---|
| hard-constraint violators surfaced | **52 / 52 (100%)** | **0 / 52 (0%)** |
| compliant items dropped by mistake | — | **0 / 94** |
| all-over-budget cases (09, 13) | 20 violations shown | empty + "nothing within budget" |

Tests: `tests/test_product_constraints.py` (21 unit tests for the filter in
isolation) + `tests/wp4/test_wp4_baseline.py` (4 acceptance tests through the
real graph: no leak, no false drops, all-violating → empty, label/checker
consistency). Full `backend_unit` suite: 607 passed.

Maps to **D3 part 1** (M2 hard eligibility gate) + the acceptance-2 test
harness. Still open on the WP4 plan:

- **D1** — formalise the case pool into a frozen snapshot + data sheet +
  missingness report; add graded relevance judgements (needed for NDCG@10).
- **D2 / B1** — classical BM25 + calibrated-popularity ranker as the middle
  baseline.
- **D3 rest** — hybrid scoring on the survivors (query/use-case + attribute
  relevance + authenticated-user affinity + calibrated popularity / head-tail),
  explanations, nearest-miss fallback instead of empty for exhausted pools.
- **Extraction** — production has no extraction step, so `resolve_constraints`
  always falls to `derive_constraints`; the `hard_constraints` key is populated
  only by the eval. Needs an LLM/rule step in the orchestrator or a graph node.
- **D5** — one-command eval reporting Recall@10 / NDCG@10 / MRR@10, p50/p95
  latency, per-slice (complex/keyword, cold/warm, budget/brand/model, missing
  price). Current harness reports violation rate + false-drop only.
- Price-missing items (~1 in 10) are treated as "unknown → allowed" — confirm
  this is the intended policy or surface them flagged.
