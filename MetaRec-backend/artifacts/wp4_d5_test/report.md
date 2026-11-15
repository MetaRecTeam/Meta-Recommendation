# WP4 D5 evaluation: product domain (SEALED TEST SPLIT, opened once with --open-test-split)

| Item | Value |
|---|---|
| Fixture | `eval/product_test_v1.jsonl` (sha256 683a0a30bc02f4ac..., NOT verified against checksums.sha256) |
| Cases scored | 443,817 (held-out users: 443,817 cases, 117,207 users) |
| Systems | B0 legacy (train-only ratings), B0 reviews only, B1 (BM25 0.25 + popularity), ItemKNN, M2 (domain_v1) |
| M2 weights | text_relevance 0.25, attribute_match 0.0, collaborative 0.05, popularity 0.65, tail_adjustment 0.05 |
| Candidate pool | real item + 99 random negatives per case (seed 42) |
| Bootstrap | user-level, 2,000 resamples, seed 42, 95% percentile interval |
| Head threshold | 18 train reviews (80th percentile) |
| Code | git 789f059, python 3.10.11, Windows |
| Run | 2026-11-09 15:46 UTC, scoring pass 21.5 min |
| Declared hardware | processor Intel64 Family 6 Model 140 Stepping 1, GenuineIntel, logical cpus 8, ram gb 15.7, os Windows 10, python 3.10.11 |
| Pools | every system ranks the same pool; metrics are conditional on the real item having been retrieved (443,817 of 443,817 held-out cases) |

## 1. Decision rule (frozen before any result was seen)

| Criterion | Result | Status |
|---|---|---|
| M2 over the stronger of B0 / B1, which is B1 (BM25 0.25 + popularity); NDCG@10 lower bound above 0 | +1.87% [+1.70%, +2.04%] | PASS |
| point estimate at least +3% | +1.87% | FAIL |
| complex slice not worse than -2% (lower bound) | +3.20% [+2.95%, +3.46%] | PASS |

**Verdict: the +3% target is NOT MET.** The comparison is with the stronger of B0 legacy (train-only ratings), B0 reviews only, B1 (BM25 0.25 + popularity); the strongest baseline overall, ItemKNN included, is B1 (BM25 0.25 + popularity). The +5% relative lift the proposal calls directional is not reached.

### Acceptance evidence (the proposal's bullets)

| Requirement | This run | Status |
|---|---|---|
| Feature-off output equals the B0 strict-API fixtures; every ranked or excluded id comes from the input; no network | acceptance tests not run (`--skip-acceptance-tests`) | NOT RUN |
| Zero hard violations; unknown-price policy | constraint corpus not run (`--skip-constraints`) | NOT RUN |
| M2 NDCG@10 exceeds the stronger of B0 / B1, which is B1 (BM25 0.25 + popularity), with a positive lower bound | +1.87%, 95% [+1.70%, +2.04%] | PASS |
| Frozen minimum effect +3% on the point estimate | +1.87% | FAIL |
| Directional reference: +5% relative lift (not the pass mark) | +1.87% | not reached |
| Complex-query slice improves or is non-inferior (margin -2% on the lower bound) | +3.20%, 95% [+2.95%, +3.46%] | PASS |
| Head / tail exposure reported, not only aggregate accuracy | M2 head share of top 10 89.9%, catalogue coverage 78.0% (section 4) | reported |
| Ranker-only p95 at most 25 ms | latency not run (`--skip-latency`) | NOT RUN |
| Cold and sparse histories always return a valid text / popularity fallback (ten distinct candidates) | B1 (BM25 0.25 + popularity): 100.0% of cases, cold 100.0%, sparse 100.0%; M2 (domain_v1): 100.0% of cases, cold 100.0%, sparse 100.0% | PASS |

## 2. Held-out users, all slices

### all held-out cases: 443,817 cases, 117,207 users

| System | Recall@10 | NDCG@10 | MRR@10 |
|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0436 | 0.0278 | 0.0230 |
| B0 reviews only | 0.2523 | 0.1531 | 0.1227 |
| B1 (BM25 0.25 + popularity) | 0.2755 | 0.1707 | 0.1385 |
| ItemKNN | 0.1945 | 0.1120 | 0.0869 |
| M2 (domain_v1) | 0.2790 | 0.1739 | 0.1416 |

### cold (no history): 137,595 cases, 15,737 users

| System | Recall@10 | NDCG@10 | MRR@10 |
|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0295 | 0.0190 | 0.0158 |
| B0 reviews only | 0.1687 | 0.1035 | 0.0835 |
| B1 (BM25 0.25 + popularity) | 0.1687 | 0.1035 | 0.0835 |
| ItemKNN | 0.1687 | 0.1035 | 0.0835 |
| M2 (domain_v1) | 0.1687 | 0.1035 | 0.0835 |

### warm (has history): 306,222 cases, 101,470 users

| System | Recall@10 | NDCG@10 | MRR@10 |
|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0499 | 0.0318 | 0.0263 |
| B0 reviews only | 0.2899 | 0.1754 | 0.1403 |
| B1 (BM25 0.25 + popularity) | 0.3234 | 0.2008 | 0.1632 |
| ItemKNN | 0.2061 | 0.1158 | 0.0884 |
| M2 (domain_v1) | 0.3285 | 0.2055 | 0.1677 |

### keyword-like (1 to 51 words of history): 107,349 cases, 23,046 users

| System | Recall@10 | NDCG@10 | MRR@10 |
|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0454 | 0.0291 | 0.0241 |
| B0 reviews only | 0.2630 | 0.1604 | 0.1290 |
| B1 (BM25 0.25 + popularity) | 0.2802 | 0.1727 | 0.1397 |
| ItemKNN | 0.1574 | 0.0857 | 0.0641 |
| M2 (domain_v1) | 0.2817 | 0.1731 | 0.1397 |

### complex (more than 51 words of history): 198,873 cases, 78,424 users

| System | Recall@10 | NDCG@10 | MRR@10 |
|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0523 | 0.0333 | 0.0275 |
| B0 reviews only | 0.3045 | 0.1835 | 0.1464 |
| B1 (BM25 0.25 + popularity) | 0.3467 | 0.2160 | 0.1759 |
| ItemKNN | 0.2324 | 0.1320 | 0.1015 |
| M2 (domain_v1) | 0.3538 | 0.2229 | 0.1827 |

### head target (18+ train reviews): 151,526 cases, 76,079 users

| System | Recall@10 | NDCG@10 | MRR@10 |
|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0599 | 0.0500 | 0.0469 |
| B0 reviews only | 0.7391 | 0.4484 | 0.3593 |
| B1 (BM25 0.25 + popularity) | 0.7586 | 0.4782 | 0.3918 |
| ItemKNN | 0.4563 | 0.2702 | 0.2130 |
| M2 (domain_v1) | 0.7431 | 0.4718 | 0.3882 |

### tail target (fewer than 18 train reviews): 292,291 cases, 89,917 users

| System | Recall@10 | NDCG@10 | MRR@10 |
|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0351 | 0.0164 | 0.0107 |
| B0 reviews only | 0.0000 | 0.0000 | 0.0000 |
| B1 (BM25 0.25 + popularity) | 0.0250 | 0.0113 | 0.0072 |
| ItemKNN | 0.0588 | 0.0300 | 0.0215 |
| M2 (domain_v1) | 0.0383 | 0.0194 | 0.0137 |

### target price missing: 53,204 cases, 34,298 users

| System | Recall@10 | NDCG@10 | MRR@10 |
|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0325 | 0.0174 | 0.0128 |
| B0 reviews only | 0.1500 | 0.0850 | 0.0650 |
| B1 (BM25 0.25 + popularity) | 0.1780 | 0.1023 | 0.0792 |
| ItemKNN | 0.1402 | 0.0774 | 0.0584 |
| M2 (domain_v1) | 0.1877 | 0.1093 | 0.0853 |

## 3. Relative lift of M2 (95% user-level bootstrap interval)

### M2 over B0 legacy (train-only ratings)

| Slice | Recall@10 | NDCG@10 | MRR@10 |
|---|---|---|---|
| all held-out cases | +539.82% [+530.24%, +550.01%] | +524.63% [+514.47%, +535.19%] | +514.26% [+502.76%, +525.43%] |
| cold (no history) | +471.31% [+450.54%, +493.06%] | +445.20% [+424.10%, +468.00%] | +428.56% [+405.68%, +453.33%] |
| warm (has history) | +558.04% [+547.34%, +569.72%] | +545.94% [+534.43%, +558.15%] | +537.40% [+524.49%, +550.76%] |
| keyword-like (1 to 51 words of history) | +520.08% [+502.60%, +540.40%] | +495.73% [+477.16%, +516.79%] | +480.69% [+460.24%, +503.09%] |
| complex (more than 51 words of history) | +575.82% [+562.37%, +590.36%] | +569.59% [+554.95%, +584.98%] | +564.17% [+547.37%, +580.81%] |
| head target (18+ train reviews) | +1140.20% [+1114.50%, +1166.82%] | +844.19% [+823.01%, +865.93%] | +727.54% [+708.00%, +747.53%] |
| tail target (fewer than 18 train reviews) | +9.12% [+6.00%, +12.43%] | +18.55% [+14.82%, +22.26%] | +28.61% [+23.75%, +33.38%] |
| target price missing | +477.56% [+450.82%, +508.68%] | +529.53% [+496.72%, +568.64%] | +565.66% [+525.95%, +612.22%] |

### M2 over B0 reviews only

| Slice | Recall@10 | NDCG@10 | MRR@10 |
|---|---|---|---|
| all held-out cases | +10.54% [+10.17%, +10.92%] | +13.55% [+13.12%, +13.94%] | +15.40% [+14.87%, +15.89%] |
| cold (no history) | +0.04% [-0.07%, +0.16%] | +0.05% [-0.01%, +0.11%] | +0.06% [+0.02%, +0.10%] |
| warm (has history) | +13.29% [+12.82%, +13.76%] | +17.13% [+16.59%, +17.62%] | +19.50% [+18.82%, +20.12%] |
| keyword-like (1 to 51 words of history) | +7.09% [+6.37%, +7.87%] | +7.90% [+7.09%, +8.78%] | +8.35% [+7.29%, +9.46%] |
| complex (more than 51 words of history) | +16.18% [+15.61%, +16.77%] | +21.48% [+20.80%, +22.12%] | +24.79% [+23.96%, +25.60%] |
| head target (18+ train reviews) | +0.54% [+0.29%, +0.79%] | +5.21% [+4.91%, +5.52%] | +8.03% [+7.61%, +8.45%] |
| tail target (fewer than 18 train reviews) | n/a (baseline 0.0000) | n/a (baseline 0.0000) | n/a (baseline 0.0000) |
| target price missing | +25.09% [+23.44%, +26.88%] | +28.58% [+26.82%, +30.62%] | +31.16% [+28.75%, +33.75%] |

### M2 over B1 (BM25 0.25 + popularity)

| Slice | Recall@10 | NDCG@10 | MRR@10 |
|---|---|---|---|
| all held-out cases | +1.27% [+1.09%, +1.46%] | +1.87% [+1.70%, +2.04%] | +2.22% [+2.01%, +2.44%] |
| cold (no history) | +0.02% [-0.03%, +0.08%] | +0.01% [-0.01%, +0.04%] | +0.01% [-0.01%, +0.02%] |
| warm (has history) | +1.56% [+1.34%, +1.80%] | +2.29% [+2.09%, +2.50%] | +2.73% [+2.47%, +3.00%] |
| keyword-like (1 to 51 words of history) | +0.50% [+0.10%, +0.89%] | +0.21% [-0.16%, +0.57%] | +0.02% [-0.46%, +0.48%] |
| complex (more than 51 words of history) | +2.03% [+1.77%, +2.31%] | +3.20% [+2.95%, +3.46%] | +3.89% [+3.58%, +4.23%] |
| head target (18+ train reviews) | -2.04% [-2.18%, -1.90%] | -1.33% [-1.46%, -1.20%] | -0.92% [-1.11%, -0.74%] |
| tail target (fewer than 18 train reviews) | +53.43% [+51.08%, +55.79%] | +72.26% [+69.75%, +74.75%] | +90.99% [+87.39%, +94.66%] |
| target price missing | +5.47% [+4.62%, +6.32%] | +6.75% [+5.97%, +7.54%] | +7.66% [+6.68%, +8.66%] |

### M2 over ItemKNN

| Slice | Recall@10 | NDCG@10 | MRR@10 |
|---|---|---|---|
| all held-out cases | +43.40% [+42.50%, +44.36%] | +55.23% [+54.21%, +56.29%] | +62.95% [+61.73%, +64.18%] |
| cold (no history) | +0.02% [-0.03%, +0.08%] | +0.01% [-0.01%, +0.04%] | +0.01% [-0.01%, +0.02%] |
| warm (has history) | +59.35% [+58.11%, +60.63%] | +77.41% [+76.00%, +78.87%] | +89.67% [+87.88%, +91.50%] |
| keyword-like (1 to 51 words of history) | +78.92% [+75.78%, +82.10%] | +101.87% [+98.39%, +105.44%] | +118.11% [+114.06%, +122.45%] |
| complex (more than 51 words of history) | +52.19% [+50.85%, +53.54%] | +68.83% [+67.33%, +70.35%] | +79.99% [+78.05%, +81.93%] |
| head target (18+ train reviews) | +62.86% [+61.87%, +63.95%] | +74.63% [+73.48%, +75.87%] | +82.19% [+80.80%, +83.65%] |
| tail target (fewer than 18 train reviews) | -34.84% [-36.38%, -33.24%] | -35.36% [-36.90%, -33.78%] | -36.03% [-37.82%, -34.24%] |
| target price missing | +33.90% [+31.03%, +36.78%] | +41.18% [+38.08%, +44.29%] | +45.98% [+42.31%, +49.75%] |

## 4. Exposure and coverage (held-out users, top 10)

| System | Head share of top 10 | Mean log(1+reviews) | Distinct items shown | Catalogue coverage |
|---|---|---|---|---|
| B0 legacy (train-only ratings) | 12.5% | 2.29 | 17,091 | 21.3% |
| B0 reviews only | 100.0% | 4.32 | 17,020 | 21.2% |
| B1 (BM25 0.25 + popularity) | 94.5% | 4.21 | 53,763 | 67.1% |
| ItemKNN | 56.8% | 3.16 | 80,181 | 100.0% |
| M2 (domain_v1) | 89.9% | 4.14 | 62,541 | 78.0% |

## 5. Data coverage of the candidate pools

- Candidates with a parsed price: 66.7%; cases where no candidate has a price: 0.
- Candidates found in the item catalogue: 100.0%.
- Provider candidate recall (reported separately, not part of any ranker metric): 100.00% of cases had the real item in the pool (0 did not). Offline this is true by construction: every pool is built around the real item, so it checks the fixture and says nothing about any provider.
- Cases that carry a machine-checkable `strict_constraints` entry: 0. The offline cases state no budget, brand or model, so hard-constraint violation counts come from the labelled searches in section 6.

## 6. Labelled constraint searches (tests/wp4/cases)

Not run (`--skip-constraints`).

## 7. Latency

Not run (`--skip-latency`).

## 8. Live smoke test

Not run: not requested (pass --live-smoke).

## 9. Reading the numbers

- Case construction: a case is one real interaction; the candidates are that item plus 99 random items the user never touched. Popularity is the train review count, so nothing from the dev period or later leaks in.
- The ranker weights were tuned on the tuning users only and are not changed here. Sections 1 to 4 are on users the tuning never saw.
- The offline cases carry no stated budget, brand or model, so the constraint gate is a no-op in sections 2 to 5; section 6 is where it is tested.
- Per-case artifacts: `per_case.csv` (rank of the real item for every system, slice labels, retrieved flag, valid-top-10 flag) and `per_case.npz` (the same plus each system's top 10 as positions in the case's candidate list, so any top 10 can be rebuilt from the fixture).
