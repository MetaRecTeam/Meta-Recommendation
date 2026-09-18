# WP4 — Constraint-aware Product Recommendation: eval harness

Measures how often the pipeline surfaces **product** results that violate a hard
constraint stated in the query (budget ceiling, category, brand, compatibility).

The constraint model + filter live in production at
`langgraph_metarec/product_constraints.py`. This directory is the labeled
dataset and the end-to-end evaluation.

## Layout

| file | role |
|---|---|
| `cases/*.json` | one query per file; each candidate hand-labeled with `expect_violations` (`[]` = compliant). |
| `dataset.py` | loads the cases; `preferences_with_constraints` = what a perfect extraction step would hand the filter. |
| `capture.py` | `python -m tests.wp4.capture "<query>" --pref k=v --out cases/X.json` — pull a live candidate pool. |
| `eval_constraints.py` | runs every case through the real graph (fake search adapter), prints B1; `--baseline` prints B0. |
| `test_wp4_baseline.py` | acceptance tests: no leak, no false drops, all-violating → empty, label/checker consistency. |

## Run

```bash
# from MetaRec-backend/, with the venv active
python -m tests.wp4.eval_constraints             # B1 report (filter on)
python -m tests.wp4.eval_constraints --baseline  # B0 report (no filter)
python -m pytest tests/wp4/ tests/test_product_constraints.py -q
```

## Adding a case

Copy `cases/case_01_keyboard.json`. Candidate dicts must match the
`amazon.product.search` adapter output (`title, brand, price, rating, reviews,
link, thumbnail, source`). Set `expect_violations` on each candidate to the
constraint keys a human judges it to break (`[]` = compliant). Keep
`hard_constraints` and the labels consistent — `test_labels_match_checker` fails
on drift between the labels and `product_constraints.violations()`.

Prefer real captures: `python -m tests.wp4.capture` against the live system,
then label. Synthetic pools are fine for coverage but note it in `notes`.

## The acceptance target

`test_no_hard_constraint_violation_is_surfaced` — with the filter on, zero
labeled violators reach the user. B0 (no filter) leaked 52/52; B1 leaks 0/52.
`test_filter_does_not_drop_compliant_items` guards the other direction.

## D0 audit and B0 freeze

| file | role |
|---|---|
| `audit_candidates.py` | measures field presence, price format, brand, and ASIN recoverability over the live captures |
| `recheck_stability.py` | re-runs captured queries live (spends SerpApi credits) and compares with the frozen copies |
| `freeze_b0.py` | writes the B0 strict-API payload for every case to `golden/` |
| `test_b0_golden.py` | feature-off output must equal `golden/`; also proves the fixtures detect a change |
