# WP4: constraint-aware product ranking — M2 hard-eligibility gate (behind flag)

Draft. First slice of WP4 (Constraint-Aware Product Recommendation,
[proposal](https://github.com/Hanny658/FYP26-Proposals/tree/main/WP4-product-recommendation)).
Everything here is **off by default** — `METAREC_PRODUCT_RANKER` defaults to `legacy`
and product output is unchanged.

## What's in this PR

**D3 (part 1) — the M2 hard-eligibility gate.** `langgraph_metarec/product_constraints.py`:

- `ProductConstraints` + `violations(item, c)` — checks an item against budget /
  category / brand / model / attribute constraints. Deterministic, no network.
- `resolve_constraints(query, preferences)` — uses an explicit
  `preferences["hard_constraints"]` when present; otherwise derives a
  conservative set from the normalized preference fields + the query text.
- `apply_hard_constraints(items, c)` — drops violators before ranking. If that
  empties the pool, relaxes the **non-budget** constraints one rung at a time
  (`attributes → exclusions → category → brand`); budget is never auto-relaxed,
  so an all-over-budget pool returns empty + an explained error.

Wired into `generic_graph.normalize_and_rank`, **only when
`METAREC_PRODUCT_RANKER != "legacy"`**. Outcome is recorded in
`metadata["constraint_filter"]` (`dropped`, `relaxed`, `exhausted`).

**D0 / D1 groundwork.** `tests/wp4/`:

- 15-case labelled corpus (`cases/*.json`) — 12 constraint queries + 3 clean
  controls, each candidate hand-labelled with `expect_violations`. Pools are
  live SerpApi captures (`capture.py`).
- `eval_constraints.py` — runs every case through the real graph;
  `--baseline` reproduces B0.
- `docs/wp4/project-log.md` — B0 audit, constraint/price/brand-missingness
  findings, mapping to the proposal deliverables.

## Results (`python -m tests.wp4.eval_constraints`)

| | B0 (default) | M2 gate on |
|---|---|---|
| hard-constraint violators surfaced | 52 / 52 (100%) | **0 / 52 (0%)** |
| compliant items dropped by mistake | — | **0 / 94** |
| all-over-budget pools (SSD, gaming laptop) | 20 violations shown | empty + "nothing within budget" |

## Acceptance criteria touched

- **(1) feature-off == B0, offline** — `test_feature_off_matches_b0_ordering`
  asserts byte-identical ordering with the flag unset / `b0`, over all 15 cases.
- **(2) zero hard-constraint violations on the labelled corpus** —
  `test_no_hard_constraint_violation_is_surfaced`.

## Not in this PR (later WP4 work)

B1 classical baseline · M2 hybrid scoring (affinity, calibrated popularity,
head/tail) · graded relevance judgements + NDCG@10 / Recall@10 / MRR@10 · latency
harness · the production extraction step that populates `hard_constraints`.

## Tests

`python -m pytest -m backend_unit` → 609 passed. New: 21 unit tests
(`tests/test_product_constraints.py`) + 7 end-to-end (`tests/wp4/`).
