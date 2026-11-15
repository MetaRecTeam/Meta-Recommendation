# WP4 · Constraint-Aware Product Recommendation for MetaRec

[简体中文](README-zh.md) | **English**

| | |
|---|---|
| **Team size** | 1 student |
| **Difficulty** | medium (●●●○○) |
| **Domain** | product |
| **Headline evidence** | complex-query NDCG/Recall, zero hard-constraint violations, head/tail coverage |

## Background and why choose this project

Product recommendation is not just “people also bought”. A request such as
“a quiet mechanical keyboard below S$120 for Mac” combines intended use,
technical attributes, brand/model language and strict budget constraints.
Popular products can be completely wrong, and missing or malformed prices must
not be treated as free.

This project gives one student a practical path through information retrieval,
implicit-feedback recommendation, schema/price normalisation, constraint-aware
ranking and robust API integration. It produces both a measurable research
comparison and a feature users can see. The core can run on a laptop; large
semantic encoders are comparative stretch work rather than a dependency.

## Research question and scope

Can a constraint-first hybrid of complex-query text relevance, user affinity
and calibrated popularity rerank MetaRec's existing product candidates better
than the generic ranker without violating explicit budget, brand, category or
model requirements?

The project does not replace Amazon/SerpAPI-style retrieval, crawl products,
predict price, alter routing/itinerary logic or generate unsupported product
attributes. It ranks frozen candidates and integrates after existing
normalisation. One frozen Amazon Reviews 2023 category/subset is enough for the
offline study.

## Existing implementation strategy and baselines

The current provider query, enrichment, normalisation, deduplication and generic
ranking remain the feature-off **B0** and are not deleted.

| ID | System |
|---|---|
| **B0 — generic** | Existing generic score/order on the shared candidate snapshots. |
| **B1 — classical** | BM25/text relevance plus calibrated popularity; add ItemKNN/EASE or BPR only where the frozen subset has sufficient interactions. |
| **M2 — constraint-aware hybrid** | Hard eligibility first, then query/use-case and attribute relevance + authenticated-user affinity when available + calibrated popularity/head-tail control. |

Compare M2 components and a text-only variant. Sparse users fall back to text
and popularity. Missing features remain unknown and are handled by declared
constraint policy, not numeric zero.

## Complete runtime input contracts

Unknown top-level fields are rejected. Timestamps are RFC 3339 UTC. `user_id`
is derived from authentication and is never accepted from request JSON.

### `RankerRequestV1`

| Field | Type and rule |
|---|---|
| `schema_version` | constant `ranker-request.v1` |
| `request_id` | recommendation-request correlation ID |
| `task_id` | current MetaRec task ID |
| `domain` | constant `product` |
| `query` | exact current product request |
| `fused_preferences` | typed use-case/category/brand/model preferences and current hard constraints |
| `candidates` | bounded, normalised and deduplicated `RecommendationItemInternal[]` |
| `interactions` | chronological `ItemInteractionV1[]`; may be empty |
| `top_k` | bounded requested count |
| `seed` | integer for stochastic components and tie-breaking |

Every `RecommendationItemInternal` has exactly: `id`, `domain`, `title`,
`subtitle`, `description`, `image_url`, `url`, `rating`, `reviews_count`,
`source`, `tags`, `why`, `gps_coordinates`, and `raw`. Optional absence is null.
Validated `raw` may supply ASIN/provider ID, brand, model, category, attributes,
price amount/currency, availability and provider popularity. Unverified text is
not promoted to a hard-filter field.

`RecommendationItemInternal` is this proposal's name for a shape that already exists — do not go looking for that class. It is `RecommendationItem` in `service.py`, the internal item the graph passes around; the public, stricter one is `RecommendationItemAPI` in `main.py`, which is the same field list **minus `raw`**. The two names matter because `raw` is exactly where your provider-specific features live and exactly what must never reach the client.

### `ItemInteractionV1`

| Field | Type and rule |
|---|---|
| `event_id` | unique idempotency ID |
| `domain` | constant `product` |
| `item_id` | canonical ID previously shown |
| `action` | `save`, `hide`, `positive`, `negative`, or `consumed` |
| `result_id` | result that showed the item |
| `occurred_at` | server-recorded timestamp |

**This seam already exists — you consume it, you do not build it.** MetaRec
ships an item-level interaction store and UI (commit `f9276a9` on
`feat/itinerary`, "item-level interaction seam"). Registered users see
**Save / Not interested / Purchased** chips under every generic result card;
guests do not. Do not add a second control or a second table, and do not
relabel the result-level `/api/feedback` as item feedback — it still has no
item id.

Where it lives and what you call:

| Piece | Location |
|---|---|
| Table and migration | `item_interactions`, `alembic/versions/20260821_0007_item_interactions.py` |
| Your read seam | `business_repositories.item_interaction_repository.list_for_user(user_id, domain="product")` — chronological, active rows only by default; `include_revoked=True` for the full audit trail |
| Wire projection | `business_models.to_interaction_v1(record)` gives exactly the `ItemInteractionV1` shape above plus `schema_version="item-interaction.v1"`; it carries no `user_id` and no payload, so it is safe to dump into your evaluator fixtures |
| HTTP (UI and smoke tests) | `POST/GET /api/item-interactions`, `DELETE /api/item-interactions/{event_id}`, `GET /api/item-interactions/options?domain=product` |
| UI control | `MetaRec-ui/src/ui/ItemInteractionControls.tsx`, mounted from `GenericItemsSection` in `Chat.tsx` |
| Contract tests | `tests/test_item_interactions_api.py` (unit) and `tests/test_item_interactions_pg.py` (Postgres) — read them; they are the executable spec |

Semantics you must model correctly, because they are enforced in the
database, not just documented:

- `event_id` is the idempotency key. Replaying one returns the stored row.
- `save` and `hide` are **toggles**: at most one *active* row each per
  (user, domain, item), and they are mutually exclusive — saving un-hides,
  hiding un-saves. Undo sets `revoked_at`; nothing is hard-deleted. A partial
  unique index guarantees this even under concurrent taps.
- `positive`, `negative` and `consumed` are **append-only events**. Two taps
  on "Purchased" are two rows with two timestamps. That is deliberate: your
  ranker needs the repeat, so never collapse them when you load history.
- `positive`/`negative` are accepted by the API but have **no chip yet**. Do
  not assume you will see them in real data; design for the three that exist.
- Each row keeps a bounded `payload.item` snapshot (`title`, `subtitle`,
  `source`, `url`) and the `result_id`/`task_id` it was shown in, so you can
  rebuild an offline dataset without re-calling providers. The provider `raw`
  blob is never stored there.

Empty history is still a required cold-start input, and the identity caveat
stands: `item_id` is whatever `generic_graph._item()` assigned, which for
product is only as stable as the provider id it was derived from. Your
identity section decides how two ids for the same thing are reconciled — the
store does not do that for you.

## Complete runtime output contract — `RankerResultV1`

| Field | Type and rule |
|---|---|
| `schema_version` | constant `ranker-result.v1` |
| `request_id` | exact input request ID |
| `ranked` | ordered `{item_id, rank, score, score_components, reason_codes}[]` |
| `excluded` | `{item_id, reason_code}[]`; accounts for every unranked candidate |
| `model_name`, `model_version`, `feature_version` | pinned identifiers |
| `warnings` | missing field/currency, mapping, cold-start and fallback codes |
| `latency_ms` | ranker-only monotonic duration |

Ranked IDs are unique and input-bound. Required components are `eligibility`,
`text_relevance`, `attribute_match`, `collaborative`, `popularity` and
`tail_adjustment`. Reason codes are allow-listed, including `USE_CASE_MATCH`,
`ATTRIBUTE_MATCH`, `HISTORY_AFFINITY`, `POPULAR_FALLBACK`, `OVER_BUDGET`,
`WRONG_BRAND`, `WRONG_CATEGORY`, `WRONG_MODEL` and
`UNKNOWN_PRICE_STRICT_MODE`. A deterministic template writes positive reasons
to the existing `why`; no model-generated product claim is allowed.

## Product identity, prices and strict API integration

Prefer ASIN when available, then stable provider product ID, then a conservative
normalised `(brand, model)` identity. Never merge by title alone. Prices are a
typed `(amount, currency)` pair with parser provenance. Missing/failed parse is
`unknown`, never zero. For a maximum-budget request, different currency is
comparable only under a pinned exchange-table version; otherwise strict mode
excludes it with an explicit reason. The evaluator uses one declared currency.

Insert the adapter after provider normalisation/deduplication and before current
top-10/public projection. It only reorders or excludes input candidates, joins
back to untouched internal items and validates the existing strict
`RecommendationItemAPI`. Scores/components remain in server experiment
metadata or Debug Arena; no new public field is introduced.

Concretely, that seam is one line in
`langgraph_metarec/graphs/generic_graph.py`:

```python
state["items"] = _rank_items(items)[:10]
```

`_rank_items` de-duplicates via `_same_recommendation` /
`_merge_recommendation_items` and then sorts by `_item_score`, which is the
tuple `(rating, reviews_count, raw["popularity"], title)`. That sort **is** B0 —
reproduce it exactly before you try to beat it. Your ranker replaces the sort,
not the de-duplication, and the `[:10]` cut stays after you.

For injection, prefer the mechanism the graph already has:
`GenericGraphAdapters` is the dataclass through which `tool_registry` and the
gather `reasoner` are already passed in, so adding an optional `ranker` field
there keeps the flag-off path byte-identical and keeps your tests free of
monkeypatching. `run_generic_domain_graph` is the entry point that builds it.

Feature flag `METAREC_PRODUCT_RANKER` accepts exactly `legacy|domain_v1`,
defaults to `legacy`, and rejects invalid startup values. Legacy bypasses the
ranker and interaction access and exactly reproduces frozen B0 fixtures. Error,
timeout, empty output, duplicate or unknown ID falls back to original order.
Current budget, required/excluded brand, category and model are hard eligibility
rules before scoring. Profile/history can never override them.

## Deliverables and workload

1. **D0 — Interim Report.** Mandatory: trace router → product tool parameters →
   providers/enrichment → normalisation/deduplication → generic rank → strict
   API/UI; reproduce B0; audit item feedback, IDs, prices and missing fields;
   freeze contracts, dataset/category, split and constraint fixtures. A BM25
   baseline, price parser or adapter fixture is optional preliminary work.
2. **D1 — Data/features.** Freeze a licensed Amazon Reviews 2023 category
   subset and provider-style candidates; publish data sheet, ID/price parser,
   missingness report and leakage-safe split.
3. **D2 — Baselines.** Reproduce B0 and tune text/popularity and one feasible
   collaborative baseline on development data only.
4. **D3 — M2 ranker.** Hard constraint engine, complex-query/attribute features,
   hybrid score, cold fallback, reason codes and ablations.
5. **D4 — Integration.** Contracts, a read adapter over the shipped item-interaction seam, feature flag,
   strict projection, fallback and network-free tests.
6. **D5 — Evaluation.** One-command report, per-case artifacts, user-bootstrap
   intervals, keyword/complex and head/tail slices, latency and live smoke.

Suggested schedule: S1 weeks 1–4 system/data audit, 5–8 B0/B1, 9–13 M2;
S2 weeks 1–5 integration/constraint fixtures, 6–10 evaluation, 11–13 report.

## Evaluation interface

Each `DomainEvalCaseV1` JSONL row contains `case_id`, anonymous `user_key`,
`cutoff_time`, `query`, `fused_preferences`, frozen `candidates`, chronological
`history`, future `ground_truth_item_ids`, machine-checkable
`strict_constraints` and `slice_labels`. Candidate features include provenance
and missingness. Record whether the target was retrieved.

```text
python -m eval.domain_ranker --domain product --fixture eval/product_v1.jsonl \
  --systems generic classical domain_v1 --out artifacts/product-eval
```

Use a chronological split within one declared category/subset. Report provider
candidate recall separately and conditional Recall@10, NDCG@10 and MRR@10 on
shared eligible pools. Report hard-constraint violation count/rate, catalogue
coverage, head/tail exposure, popularity, mapping/price-parse coverage and
p50/p95 latency. Slice by complex versus keyword query, cold/warm user,
head/tail target, budget/brand/model constraint and missing price. Bootstrap
users; fixture/provider snapshots, not live SerpAPI availability, decide the
locked result.

### Acceptance evidence

- Feature-off output exactly matches B0 strict API fixtures; every ranked or
  excluded ID comes from input and tests run without network access.
- Zero hard budget/brand/category/model violations in the labelled constraint
  corpus; unknown price/currency always follows the declared strict policy.
- M2 conditional NDCG@10 exceeds the stronger B0/B1 under a supervisor-frozen
  user-bootstrap minimum effect; 5% relative lift is directional.
- M2 improves or is non-inferior on the complex-query slice under a pre-frozen
  margin and reports head/tail exposure, not only aggregate accuracy.
- Ranker-only p95 ≤25 ms for 100 candidates on declared hardware; cold/sparse
  histories always return a valid text/popularity fallback.

## Data and reading list

Use one frozen, documented category/subset from **Amazon Reviews 2023/BLaIR**
under its published access and licence conditions. Store checksums and the
exact processing script; never publish MetaRec user interaction data.

Shared recommender core (four papers):

1. Covington et al., **Deep Neural Networks for YouTube Recommendations**
   (RecSys 2016), [Google Research](https://research.google/pubs/deep-neural-networks-for-youtube-recommendations/) — candidate generation versus ranking.
2. Rendle et al., **BPR** (UAI 2009), [arXiv](https://arxiv.org/abs/1205.2618)
   — pairwise implicit-feedback ranking.
3. Steck, **Embarrassingly Shallow Autoencoders for Sparse Data** (WWW 2019),
   [arXiv](https://arxiv.org/abs/1905.03375) — strong sparse baseline.
4. Dacrema et al., **Are We Really Making Much Progress?** (RecSys 2019),
   [arXiv](https://arxiv.org/abs/1907.06902) — reproducible comparison discipline.

Product-specific (one paper):

5. Hou et al., **Bridging Language and Items for Retrieval and
   Recommendation: Benchmarking LLMs as Semantic Encoders** (ACL 2026),
   [ACL Anthology](https://aclanthology.org/2026.acl-long.147/) — Amazon Reviews
   2023, sequential/collaborative/product-search tasks and complex-query
   evaluation. Pretrained BLaIR encoders are optional comparisons, not required.

## Coding agents versus independent thinking

Coding agents can build parsers, BM25 and metric pipelines. The viva tests why
a constraint is hard, how unknown prices/currencies behave, identity and
temporal leakage, retrieval ceiling, popularity bias and whether explanations
are grounded in actual candidate fields.
