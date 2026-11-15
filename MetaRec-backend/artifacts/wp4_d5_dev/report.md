# WP4 D5 evaluation: product domain (development split)

| Item | Value |
|---|---|
| Fixture | `eval/product_dev_v1.jsonl` (sha256 85a5d9536f57cdfd..., matches the frozen checksum) |
| Cases scored | 534,128 (held-out users: 479,889 cases, 134,481 users) |
| Systems | B0 legacy (train-only ratings), B0 reviews only, B1 (BM25 0.25 + popularity), ItemKNN, M2 (domain_v1) |
| M2 weights | text_relevance 0.25, attribute_match 0.0, collaborative 0.05, popularity 0.65, tail_adjustment 0.05 |
| Candidate pool | real item + 99 random negatives per case (seed 42) |
| Bootstrap | user-level, 2,000 resamples, seed 42, 95% percentile interval |
| Head threshold | 12 train reviews (80th percentile) |
| Code | git bba1eb0, python 3.10.11, Windows |
| Run | 2026-11-01 08:04 UTC, scoring pass 24.9 min |
| Declared hardware | processor Intel64 Family 6 Model 140 Stepping 1, GenuineIntel, logical cpus 8, ram gb 15.7, os Windows 10, python 3.10.11 |
| Pools | every system ranks the same pool; metrics are conditional on the real item having been retrieved (479,889 of 479,889 held-out cases) |

## 1. Decision rule (frozen before any result was seen)

| Criterion | Result | Status |
|---|---|---|
| M2 over the stronger of B0 / B1, which is B1 (BM25 0.25 + popularity); NDCG@10 lower bound above 0 | +0.81% [+0.69%, +0.94%] | PASS |
| point estimate at least +3% | +0.81% | FAIL |
| complex slice not worse than -2% (lower bound) | +2.91% [+2.62%, +3.21%] | PASS |

**Verdict: the +3% target is NOT MET.** The comparison is with the stronger of B0 legacy (train-only ratings), B0 reviews only, B1 (BM25 0.25 + popularity); the strongest baseline overall, ItemKNN included, is B1 (BM25 0.25 + popularity). The +5% relative lift the proposal calls directional is not reached.

### Acceptance evidence (the proposal's bullets)

| Requirement | This run | Status |
|---|---|---|
| Feature-off output equals the B0 strict-API fixtures; every ranked or excluded id comes from the input; no network | pytest test_b0_golden.py, test_d4_live_ranker.py, test_wp4_baseline.py, test_product_ranker_m2.py: 153 passed in 6.88s | PASS |
| Zero hard budget / brand / category / model violations in the labelled corpus | 0 of 52 labelled violators surfaced (B0: 52) | PASS |
| Unknown price follows the declared strict policy (excluded when a budget is stated) | 0 unpriced items shown of 3 in the pools; 3 compliant items excluded by the policy on purpose | PASS |
| M2 NDCG@10 exceeds the stronger of B0 / B1, which is B1 (BM25 0.25 + popularity), with a positive lower bound | +0.81%, 95% [+0.69%, +0.94%] | PASS |
| Frozen minimum effect +3% on the point estimate | +0.81% | FAIL |
| Directional reference: +5% relative lift (not the pass mark) | +0.81% | not reached |
| Complex-query slice improves or is non-inferior (margin -2% on the lower bound) | +2.91%, 95% [+2.62%, +3.21%] | PASS |
| Head / tail exposure reported, not only aggregate accuracy | M2 head share of top 10 91.5%, catalogue coverage 70.3% (section 4) | reported |
| Ranker-only p95 at most 25 ms for 100 candidates on declared hardware | offline M2 sample: p95 9.3 ms; live ranker, 100 candidates: p95 10.5 ms | PASS |
| Cold and sparse histories always return a valid text / popularity fallback (ten distinct candidates) | B1 (BM25 0.25 + popularity): 100.0% of cases, cold 100.0%, sparse 100.0%; M2 (domain_v1): 100.0% of cases, cold 100.0%, sparse 100.0% | PASS |

## 2. Held-out users, all slices

### all held-out cases: 479,889 cases, 134,481 users

| System | Recall@10 | NDCG@10 | MRR@10 | graded NDCG@10 |
|---|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0776 | 0.0347 | 0.0222 | 0.0313 |
| B0 reviews only | 0.3551 | 0.2231 | 0.1825 | 0.1940 |
| B1 (BM25 0.25 + popularity) | 0.3713 | 0.2350 | 0.1929 | 0.2041 |
| ItemKNN | 0.2738 | 0.1676 | 0.1350 | 0.1471 |
| M2 (domain_v1) | 0.3737 | 0.2369 | 0.1947 | 0.2059 |

### cold (no history): 189,442 cases, 39,144 users

| System | Recall@10 | NDCG@10 | MRR@10 | graded NDCG@10 |
|---|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0755 | 0.0338 | 0.0216 | 0.0311 |
| B0 reviews only | 0.3602 | 0.2282 | 0.1876 | 0.2051 |
| B1 (BM25 0.25 + popularity) | 0.3602 | 0.2282 | 0.1875 | 0.2051 |
| ItemKNN | 0.3602 | 0.2282 | 0.1875 | 0.2051 |
| M2 (domain_v1) | 0.3600 | 0.2282 | 0.1875 | 0.2051 |

### warm (has history): 290,447 cases, 95,337 users

| System | Recall@10 | NDCG@10 | MRR@10 | graded NDCG@10 |
|---|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0789 | 0.0352 | 0.0225 | 0.0315 |
| B0 reviews only | 0.3518 | 0.2198 | 0.1792 | 0.1867 |
| B1 (BM25 0.25 + popularity) | 0.3786 | 0.2394 | 0.1965 | 0.2035 |
| ItemKNN | 0.2175 | 0.1281 | 0.1007 | 0.1092 |
| M2 (domain_v1) | 0.3827 | 0.2426 | 0.1993 | 0.2065 |

### keyword-like (1 to 51 words of history): 145,681 cases, 43,725 users

| System | Recall@10 | NDCG@10 | MRR@10 | graded NDCG@10 |
|---|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0804 | 0.0360 | 0.0231 | 0.0323 |
| B0 reviews only | 0.3622 | 0.2274 | 0.1859 | 0.1950 |
| B1 (BM25 0.25 + popularity) | 0.3816 | 0.2396 | 0.1959 | 0.2054 |
| ItemKNN | 0.1764 | 0.1033 | 0.0811 | 0.0886 |
| M2 (domain_v1) | 0.3819 | 0.2390 | 0.1950 | 0.2051 |

### complex (more than 51 words of history): 144,758 cases, 51,611 users

| System | Recall@10 | NDCG@10 | MRR@10 | graded NDCG@10 |
|---|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0773 | 0.0344 | 0.0219 | 0.0306 |
| B0 reviews only | 0.3413 | 0.2121 | 0.1724 | 0.1784 |
| B1 (BM25 0.25 + popularity) | 0.3755 | 0.2391 | 0.1971 | 0.2016 |
| ItemKNN | 0.2588 | 0.1530 | 0.1205 | 0.1300 |
| M2 (domain_v1) | 0.3834 | 0.2461 | 0.2037 | 0.2080 |

### head target (12+ train reviews): 217,221 cases, 96,279 users

| System | Recall@10 | NDCG@10 | MRR@10 | graded NDCG@10 |
|---|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0184 | 0.0110 | 0.0089 | 0.0104 |
| B0 reviews only | 0.7845 | 0.4929 | 0.4031 | 0.4285 |
| B1 (BM25 0.25 + popularity) | 0.7876 | 0.5044 | 0.4168 | 0.4382 |
| ItemKNN | 0.5368 | 0.3367 | 0.2748 | 0.2962 |
| M2 (domain_v1) | 0.7757 | 0.4978 | 0.4119 | 0.4330 |

### tail target (fewer than 12 train reviews): 262,668 cases, 107,890 users

| System | Recall@10 | NDCG@10 | MRR@10 | graded NDCG@10 |
|---|---|---|---|---|
| B0 legacy (train-only ratings) | 0.1265 | 0.0542 | 0.0331 | 0.0486 |
| B0 reviews only | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| B1 (BM25 0.25 + popularity) | 0.0270 | 0.0122 | 0.0078 | 0.0105 |
| ItemKNN | 0.0563 | 0.0278 | 0.0194 | 0.0238 |
| M2 (domain_v1) | 0.0413 | 0.0211 | 0.0150 | 0.0182 |

### target price missing: 97,284 cases, 60,352 users

| System | Recall@10 | NDCG@10 | MRR@10 | graded NDCG@10 |
|---|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0840 | 0.0355 | 0.0215 | 0.0315 |
| B0 reviews only | 0.2064 | 0.1157 | 0.0880 | 0.0981 |
| B1 (BM25 0.25 + popularity) | 0.2270 | 0.1288 | 0.0989 | 0.1091 |
| ItemKNN | 0.1776 | 0.0998 | 0.0762 | 0.0853 |
| M2 (domain_v1) | 0.2334 | 0.1342 | 0.1039 | 0.1137 |

## 3. Relative lift of M2 (95% user-level bootstrap interval)

### M2 over B0 legacy (train-only ratings)

| Slice | Recall@10 | NDCG@10 | MRR@10 | graded NDCG@10 |
|---|---|---|---|---|
| all held-out cases | +381.86% [+376.13%, +387.79%] | +583.49% [+574.14%, +592.76%] | +778.44% [+763.90%, +792.92%] | +557.47% [+548.11%, +567.24%] |
| cold (no history) | +376.70% [+367.26%, +386.65%] | +575.22% [+560.30%, +590.30%] | +766.62% [+743.92%, +789.84%] | +559.24% [+544.30%, +574.91%] |
| warm (has history) | +385.08% [+377.79%, +392.45%] | +588.67% [+577.21%, +600.34%] | +785.85% [+767.86%, +804.00%] | +556.34% [+544.73%, +568.55%] |
| keyword-like (1 to 51 words of history) | +374.75% [+364.90%, +384.88%] | +563.77% [+548.44%, +579.55%] | +745.53% [+721.96%, +769.50%] | +534.38% [+519.37%, +550.57%] |
| complex (more than 51 words of history) | +395.91% [+385.93%, +407.00%] | +614.87% [+598.81%, +632.50%] | +828.45% [+803.48%, +855.83%] | +579.65% [+563.45%, +597.96%] |
| head target (12+ train reviews) | +4114.63% [+3984.81%, +4246.77%] | +4433.01% [+4268.59%, +4595.05%] | +4510.88% [+4318.51%, +4710.44%] | +4068.15% [+3914.51%, +4221.19%] |
| tail target (fewer than 12 train reviews) | -67.32% [-68.07%, -66.52%] | -61.18% [-62.19%, -60.15%] | -54.66% [-56.09%, -53.24%] | -62.61% [-63.64%, -61.49%] |
| target price missing | +178.00% [+170.44%, +185.23%] | +277.58% [+266.52%, +288.46%] | +384.09% [+367.92%, +400.86%] | +261.35% [+250.09%, +272.48%] |

### M2 over B0 reviews only

| Slice | Recall@10 | NDCG@10 | MRR@10 | graded NDCG@10 |
|---|---|---|---|---|
| all held-out cases | +5.25% [+5.02%, +5.49%] | +6.16% [+5.91%, +6.43%] | +6.68% [+6.36%, +7.02%] | +6.17% [+5.89%, +6.46%] |
| cold (no history) | -0.05% [-0.13%, +0.02%] | -0.03% [-0.07%, +0.01%] | -0.02% [-0.05%, +0.01%] | -0.03% [-0.07%, +0.01%] |
| warm (has history) | +8.79% [+8.41%, +9.18%] | +10.36% [+9.94%, +10.81%] | +11.25% [+10.70%, +11.82%] | +10.60% [+10.14%, +11.08%] |
| keyword-like (1 to 51 words of history) | +5.46% [+4.98%, +5.94%] | +5.11% [+4.59%, +5.67%] | +4.87% [+4.15%, +5.58%] | +5.16% [+4.55%, +5.77%] |
| complex (more than 51 words of history) | +12.34% [+11.76%, +12.93%] | +16.01% [+15.34%, +16.74%] | +18.18% [+17.32%, +19.09%] | +16.59% [+15.89%, +17.37%] |
| head target (12+ train reviews) | -1.12% [-1.29%, -0.96%] | +1.00% [+0.81%, +1.21%] | +2.18% [+1.92%, +2.45%] | +1.04% [+0.83%, +1.26%] |
| tail target (fewer than 12 train reviews) | n/a (baseline 0.0000) | n/a (baseline 0.0000) | n/a (baseline 0.0000) | n/a (baseline 0.0000) |
| target price missing | +13.12% [+12.30%, +13.99%] | +15.99% [+15.02%, +16.99%] | +18.03% [+16.79%, +19.33%] | +15.93% [+14.89%, +17.02%] |

### M2 over B1 (BM25 0.25 + popularity)

| Slice | Recall@10 | NDCG@10 | MRR@10 | graded NDCG@10 |
|---|---|---|---|---|
| all held-out cases | +0.66% [+0.53%, +0.79%] | +0.81% [+0.69%, +0.94%] | +0.89% [+0.73%, +1.06%] | +0.89% [+0.76%, +1.02%] |
| cold (no history) | -0.03% [-0.07%, +0.00%] | -0.01% [-0.03%, +0.00%] | -0.01% [-0.01%, +0.00%] | -0.02% [-0.03%, +0.00%] |
| warm (has history) | +1.09% [+0.89%, +1.31%] | +1.33% [+1.13%, +1.53%] | +1.45% [+1.19%, +1.72%] | +1.48% [+1.27%, +1.69%] |
| keyword-like (1 to 51 words of history) | +0.09% [-0.18%, +0.35%] | -0.24% [-0.50%, +0.02%] | -0.46% [-0.81%, -0.11%] | -0.16% [-0.45%, +0.11%] |
| complex (more than 51 words of history) | +2.12% [+1.82%, +2.44%] | +2.91% [+2.62%, +3.21%] | +3.35% [+2.96%, +3.75%] | +3.17% [+2.85%, +3.49%] |
| head target (12+ train reviews) | -1.50% [-1.61%, -1.40%] | -1.29% [-1.39%, -1.19%] | -1.19% [-1.33%, -1.04%] | -1.20% [-1.31%, -1.09%] |
| tail target (fewer than 12 train reviews) | +52.87% [+50.55%, +55.39%] | +73.00% [+70.52%, +75.60%] | +92.78% [+89.15%, +96.73%] | +73.14% [+70.48%, +75.96%] |
| target price missing | +2.82% [+2.30%, +3.31%] | +4.15% [+3.68%, +4.63%] | +5.07% [+4.47%, +5.73%] | +4.20% [+3.71%, +4.74%] |

### M2 over ItemKNN

| Slice | Recall@10 | NDCG@10 | MRR@10 | graded NDCG@10 |
|---|---|---|---|---|
| all held-out cases | +36.50% [+35.88%, +37.11%] | +41.33% [+40.65%, +42.02%] | +44.21% [+43.44%, +44.98%] | +40.02% [+39.30%, +40.73%] |
| cold (no history) | -0.03% [-0.07%, +0.00%] | -0.01% [-0.03%, +0.00%] | -0.01% [-0.01%, +0.00%] | -0.02% [-0.03%, +0.00%] |
| warm (has history) | +75.96% [+74.60%, +77.27%] | +89.37% [+87.86%, +90.97%] | +97.92% [+96.12%, +99.83%] | +89.04% [+87.37%, +90.65%] |
| keyword-like (1 to 51 words of history) | +116.49% [+113.92%, +119.21%] | +131.29% [+128.40%, +134.41%] | +140.55% [+137.12%, +144.30%] | +131.52% [+128.32%, +134.90%] |
| complex (more than 51 words of history) | +48.15% [+46.85%, +49.59%] | +60.86% [+59.38%, +62.45%] | +69.04% [+67.17%, +70.94%] | +59.91% [+58.34%, +61.53%] |
| head target (12+ train reviews) | +44.50% [+43.82%, +45.14%] | +47.87% [+47.14%, +48.57%] | +49.91% [+49.11%, +50.71%] | +46.19% [+45.42%, +46.91%] |
| tail target (fewer than 12 train reviews) | -26.56% [-28.27%, -24.64%] | -24.19% [-26.03%, -22.09%] | -22.57% [-24.85%, -20.06%] | -23.53% [-25.57%, -21.29%] |
| target price missing | +31.48% [+29.81%, +33.10%] | +34.49% [+32.66%, +36.12%] | +36.34% [+34.29%, +38.27%] | +33.29% [+31.38%, +35.08%] |

## 4. Exposure and coverage (held-out users, top 10)

| System | Head share of top 10 | Mean log(1+reviews) | Distinct items shown | Catalogue coverage |
|---|---|---|---|---|
| B0 legacy (train-only ratings) | 5.7% | 1.61 | 17,072 | 21.3% |
| B0 reviews only | 100.0% | 3.98 | 17,000 | 21.2% |
| B1 (BM25 0.25 + popularity) | 95.3% | 3.88 | 47,285 | 59.0% |
| ItemKNN | 61.6% | 2.79 | 80,181 | 100.0% |
| M2 (domain_v1) | 91.5% | 3.82 | 56,396 | 70.3% |

## 5. Data coverage of the candidate pools

- Candidates with a parsed price: 66.6%; cases where no candidate has a price: 0.
- Candidates found in the item catalogue: 100.0%.
- Provider candidate recall (reported separately, not part of any ranker metric): 100.00% of cases had the real item in the pool (0 did not). Offline this is true by construction: every pool is built around the real item, so it checks the fixture and says nothing about any provider.
- Cases that carry a machine-checkable `strict_constraints` entry: 0. The offline cases state no budget, brand or model, so hard-constraint violation counts come from the labelled searches in section 6.

## 6. Labelled constraint searches (tests/wp4/cases)

|  | B0 (no filter) | domain_v1 (constraint-first) |
|---|---|---|
| Labelled violators in the top 10 | 52 of 52 | 0 of 52 |
| Violation rate: share of labelled violators that reach the top 10 | 100.0% | 0.0% |
| Violation rate: share of shown slots held by a violator | 35.6% | 0.0% |
| Searches with a violator in the top 3 | 9 of 15 | 0 of 15 |
| Unpriced items shown | 3 | 0 |

Compliant items dropped by mistake: 0. Compliant items excluded on purpose by the missing-price policy: 3. Unpriced items in the 15 pools: 3. Label versus checker disagreements: 0.

| Violation type | B0 | domain_v1 |
|---|---|---|
| over budget | 48 | 0 |
| wrong brand | 2 | 0 |
| wrong category | 3 | 0 |
| wrong model / required term | 4 | 0 |

Sliced by the kind of constraint a search states (a search can state several), and by missing price (for that row the counts are unpriced items, not violators):

| Constraint kind | Searches | Pool violators / unpriced | B0 top 10 | domain_v1 top 10 |
|---|---|---|---|---|
| budget | 15 | 52 | 52 | 0 |
| brand | 2 | 5 | 5 | 0 |
| model / category terms | 15 | 52 | 52 | 0 |
| missing price | 2 | 3 | 3 | 0 |

| Search | Constraints | Pool violators | B0 top 10 | domain_v1 top 10 | Unpriced in pool |
|---|---|---|---|---|---|
| case_01_keyboard | budget, model / category terms | 3 | 3 | 0 | 0 |
| case_02_keyboard_live | budget, model / category terms | 3 | 3 | 0 | 0 |
| case_03_mouse | budget, model / category terms | 0 | 0 | 0 | 0 |
| case_04_headphones | budget, model / category terms | 0 | 0 | 0 | 0 |
| case_05_sony_headphones | budget, brand, model / category terms | 3 | 3 | 0 | 1 |
| case_06_monitor | budget, model / category terms | 7 | 7 | 0 | 0 |
| case_07_earbuds | budget, brand, model / category terms | 2 | 2 | 0 | 0 |
| case_08_keyboard_tight | budget, model / category terms | 0 | 0 | 0 | 0 |
| case_09_ssd | budget, model / category terms | 10 | 10 | 0 | 0 |
| case_10_standing_desk | budget, model / category terms | 6 | 6 | 0 | 0 |
| case_11_speaker | budget, model / category terms | 3 | 3 | 0 | 2 |
| case_12_webcam | budget, model / category terms | 2 | 2 | 0 | 0 |
| case_13_gaming_laptop | budget, model / category terms | 10 | 10 | 0 | 0 |
| case_14_espresso | budget, model / category terms | 2 | 2 | 0 | 0 |
| case_15_studio_headphones | budget, model / category terms | 1 | 1 | 0 | 0 |

## 7. Latency

Ranker-only, one process, 500 dev cases (every 1068th case), 100 candidates each, on the hardware declared at the top.

| System | p50 ms | p95 ms | max ms | p95 ms, users with history |
|---|---|---|---|---|
| B0 legacy (train-only ratings) | 0.08 | 0.09 | 0.16 | 0.09 |
| B0 reviews only | 0.12 | 0.15 | 0.58 | 0.13 |
| B1 (BM25 0.25 + popularity) | 2.60 | 6.62 | 20.87 | 9.16 |
| ItemKNN | 0.42 | 0.84 | 1.93 | 1.00 |
| M2 (domain_v1) | 3.78 | 9.33 | 27.17 | 10.92 |

Live ranker (`rank_live`) on 100 provider-shaped candidates with a budget stated, 100 runs: p50 3.19 ms, p95 10.51 ms, max 18.12 ms. The target is 25 ms at p95.

## 8. Live smoke test

Not run: not requested (pass --live-smoke).

## 9. Reading the numbers

- Case construction: a case is one real interaction; the candidates are that item plus 99 random items the user never touched. Popularity is the train review count, so nothing from the dev period or later leaks in.
- The ranker weights were tuned on the tuning users only and are not changed here. Sections 1 to 4 are on users the tuning never saw.
- The offline cases carry no stated budget, brand or model, so the constraint gate is a no-op in sections 2 to 5; section 6 is where it is tested.
- Per-case artifacts: `per_case.csv` (rank of the real item for every system, slice labels, retrieved flag, valid-top-10 flag) and `per_case.npz` (the same plus each system's top 10 as positions in the case's candidate list, so any top 10 can be rebuilt from the fixture).
