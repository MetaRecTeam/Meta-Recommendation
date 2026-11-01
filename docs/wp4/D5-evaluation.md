# WP4 D5 — the one-command evaluation

```bash
# from MetaRec-backend/, venv active (about 25 minutes on the dev set)
python -m eval.domain_ranker --domain product --fixture eval/product_dev_v1.jsonl \
  --systems generic classical domain_v1 --out artifacts/wp4_d5_dev
```

`generic` is B0, `classical` is B1 and ItemKNN, `domain_v1` is M2 at the weights the live ranker uses.
One pass scores every system and keeps, for every case, the rank of the real item, each system's top 10
and the slice labels. The report is computed from that record, so any table can be traced to rows in
`per_case.csv` / `per_case.npz`.

## Files

| File | Content | In git |
|---|---|---|
| `eval/domain_ranker.py` | the evaluator | yes |
| `tests/wp4/test_d5_evaluator.py` | 41 tests, no fixture file needed | yes |
| `artifacts/wp4_d5_dev/report.md`, `report.json` | the dev-set report | yes |
| `artifacts/*/per_case.csv`, `per_case.npz` | per-case artifacts (rank per system, top 10 as candidate positions, slice labels, retrieved flag, valid-top-10 flag) | no (large; reproducible from the fixture) |

Other options: `--limit N` (a smoke test, stamped "PARTIAL" in the report), `--skip-latency`,
`--skip-constraints`, `--skip-acceptance-tests`, `--live-smoke` (needs `SERPAPI_KEY`; otherwise recorded as
"not run"), `--open-test-split` (see below).

## What each requirement of the proposal maps to

| Proposal (Evaluation interface and acceptance evidence) | Where |
|---|---|
| One-command report, per-case artifacts | the command above; `per_case.*` |
| Record whether the target was retrieved | `target_retrieved` column; a miss ranks past the pool and leaves the conditional metrics |
| Provider candidate recall, reported separately | report §5 (100% offline by construction, said so) |
| Conditional Recall / NDCG / MRR @10 on shared pools | every system ranks the same pool; report §2 |
| Hard-constraint violation count and rate | report §6: share of violators reaching the top 10 and share of slots they hold |
| Catalogue coverage, head/tail exposure, popularity | report §4 |
| Mapping and price-parse coverage | report §5 |
| p50 / p95 latency, declared hardware | report header and §7 |
| Slices: complex vs keyword, cold/warm, head/tail, budget/brand/model, missing price | §2 and §3 (offline slices incl. target price missing), §6 (constraint kinds and missing price in the labelled searches) |
| Bootstrap users | 2,000 user-level resamples, seed 42, 95% interval |
| Fixture, not live SerpApi, decides the result | the live smoke test is separate and optional |
| Feature-off equals B0; ids from input; no network | the evaluator runs the four pytest files with `SERPAPI_KEY` removed |
| Zero violations; unknown-price policy | §6 and the acceptance table |
| M2 beats the stronger of B0 / B1; 5% is directional | §1: compares with the stronger of B0 legacy, B0 reviews only and B1; +3% frozen minimum, +5% shown as directional |
| Complex slice improves or is non-inferior, margin frozen | §1: lower bound above -2% |
| p95 at most 25 ms for 100 candidates; valid cold/sparse fallback | acceptance table |

## The sealed test split

A fixture whose name marks it as the test split is refused unless `--open-test-split` is given. The flag
exists for one run at the very end, with the weights as they stand and no further tuning. On the test split
the past is train plus dev, and every user is scored (nothing was tuned on it).

## Result on the development set (held-out users, 479,889 cases, 134,481 users)

| System | Recall@10 | NDCG@10 | MRR@10 |
|---|---|---|---|
| B0 legacy (train-only ratings) | 0.0776 | 0.0347 | 0.0222 |
| B0 reviews only | 0.3551 | 0.2231 | 0.1825 |
| B1 | 0.3713 | 0.2350 | 0.1929 |
| ItemKNN | 0.2738 | 0.1676 | 0.1350 |
| **M2** | **0.3737** | **0.2369** | **0.1947** |

* **M2 over B1, NDCG@10: +0.81%, 95% [+0.69%, +0.94%].** The lower bound is above 0, but the frozen +3%
  minimum effect is **not met**, and neither is the +5% directional line.
* **Complex slice: +2.91% [+2.62%, +3.21%]**, comfortably inside the -2% non-inferiority margin. Keyword-like
  cases are level (-0.24%, interval includes 0), cold users are identical by design.
* **Head / tail:** M2 is 1.29% below B1 on head targets and 73% above it on tail targets (0.0211 vs 0.0122
  NDCG@10), with 91.5% of its top 10 from the head against 95.3% for B1, and 70.3% catalogue coverage against 59.0%.
  ItemKNN covers the whole catalogue but is far less accurate.
* **Labelled constraint searches:** 0 of 52 violators surfaced (B0: 52 of 52; 9 of 15 searches had one in the
  top 3). 3 unpriced items excluded by the missing-price policy, none shown, no compliant item dropped by mistake.
* **Latency (idle machine, Intel64 Family 6 Model 140, 8 logical CPUs, 15.7 GB):** M2 p95 9.4 ms on 500 dev cases; the live
  ranker on 100 provider-shaped candidates p95 4.1 ms. Target 25 ms.
* **Acceptance tests:** the four pytest files pass (153 tests).
* **Not run:** the live smoke test (no key given), and the sealed test split.

Reading a negative result honestly: the offline cases have no stated budget, brand or model, so the part of M2
that is new (the hard constraints) cannot show up in the accuracy numbers; it shows up in §6. The accuracy gain
over a strong popularity baseline is small and real but below the frozen minimum. The weights are not
re-tuned for the final run.

## How the evaluator was checked

* The user-level bootstrap gives the same interval as the earlier `wp4_slices.py` / `wp4_m2_tune.py` method on
  the same ranks (differences of order 1e-16), and the full run reproduces the earlier +0.81% and +2.91%.
* 24 deliberate breaks of the evaluator (split guard, held-out rule, the 51-word and head cut-offs, the
  decision-rule thresholds, the stronger-baseline choice, the live weights, rank order, the graded-label
  alignment check, the latency and fallback checks, ...) are each caught by a test.
* Graded-label rows are checked against the cases row by row; the fixture is checked against
  `eval/checksums.sha256`.

## Limits

* "Provider candidate recall" is 100% by construction offline; it checks the fixture, not any provider.
* Hardware is declared from the machine that ran the report; rerun on the demo machine if it differs.
* Ratios against a baseline that is almost zero (B0 reviews only on tail targets) are printed as huge
  percentages; read the absolute NDCG in §2 for those rows.
* The 3% minimum effect is taken from the plan as frozen earlier; if the supervisor's frozen value differs,
  change `TARGET_LIFT` and rerun.
