# WP4 D4 — M2 in the live request path

D4 puts the constraint-aware ranker (M2) behind `METAREC_PRODUCT_RANKER`. With the flag
off nothing changes. With it on, product requests are ranked by M2 after normalisation and
de-duplication and before the top-10 cut.

## What was added

| Piece | Where |
|---|---|
| Flag, strict startup validation, timeout | `langgraph_metarec/product_ranker_live.py` (`validate_product_ranker_setting`), called from `main._lifespan` |
| Contracts `RankerRequestV1`, `RankerResultV1` | `langgraph_metarec/product_ranker_live.py` |
| Live scorer (six components), fallback, `why` template, ASIN identity | `langgraph_metarec/product_ranker_live.py` |
| The seam in the graph | `generic_graph._rank_product_with_m2`, called from `normalize_and_rank` |
| Read adapter over the item-interaction seam | `service._execute_generic_domain_task` → `item_interaction_repository.list_for_user` → `history_from_records` |
| ASIN kept by the provider adapter | `tool_registry._amazon_product_search_adapter` |
| Server-side metadata, stripped for clients | `result.metadata["product_ranker"]`, `main._INTERNAL_METADATA_KEYS` |
| Tests (no network, database or key) | `tests/wp4/test_d4_live_ranker.py` |

## Behaviour

* **Flag.** Exactly `legacy` or `domain_v1`; unset or empty is `legacy`; anything else stops startup.
  The graph reads the flag exactly too (`DOMAIN_V1` or ` domain_v1` count as `legacy`).
* **Flag off.** The ranker, the history seam and the ASIN are never used. Ids stay the title hash, so
  saved items keep matching, and the output equals the frozen B0 golden files.
* **Contracts.** `RankerRequestV1` rejects unknown top-level fields (so a `user_id` can never ride in),
  needs candidates with exactly the 14 internal item fields (at most 100), validates each interaction as
  `ItemInteractionV1` (RFC 3339 time with an offset) and carries typed preferences plus the hard constraints.
  `RankerResultV1` has exactly the proposal's fields, with every unranked candidate in `excluded`.
* **Eligibility first.** The stated budget, brand, category and compatibility terms are hard rules.
  An unknown price is excluded with `UNKNOWN_PRICE_STRICT_MODE` when a budget is stated, and kept and
  flagged (`PRICE_MISSING`) when it is not. History can never override a rule.
* **Currency.** A budget carries its currency (`S$120` is SGD, `US$120` and a bare `$` are USD) and a
  price is read from its symbol. A price in another currency is excluded under the same code, because no
  exchange table is pinned. A table can be pinned in `PINNED_FX_TABLE` (versioned; the version is added to
  the warnings). A price with no symbol is kept with a `CURRENCY_MISSING` warning.
* **Scoring.** The re-tuned offline weights: text 0.25, attribute 0, collaborative 0.05, popularity 0.65,
  tail 0.05. For a live request the text is the real query plus the stated attributes, scored with BM25 over
  the candidates. Affinity is similarity to what the user saved, bought or marked positive (an exact ASIN match
  counts most), and a hidden item goes below everything else. No history is a cold start (`POPULAR_FALLBACK`).
  Only the latest 200 interactions are read, and the read is limited by the same timeout as the ranker.
* **Identity.** The ASIN (the provider field, or `/dp/<ASIN>` in the link) is the id under `domain_v1`.
  Two results with the same ASIN are one candidate.
* **Projection.** M2 only reorders or removes the items it is given and joins back to the untouched internal
  items. The only field it changes is `why`, which a fixed template fills from real fields. No new public field.
* **Fallback.** An error, a timeout, an empty output, or a duplicate, unknown or missing id returns the original
  B0 order, still behind the hard gate, with a `FALLBACK_*` warning. If the ranker itself raises, the same applies.
* **Injection.** `GenericGraphAdapters.ranker` takes the whole ranker (`rank_live`'s signature), so tests need
  no monkeypatching; `ranker_history`, `ranker_scorer` and `ranker_timeout_s` are narrower hooks.

## Measured

On the 15 labelled searches: violators shown 52 to 0, cases with a violator in the top 3 from 9 to 0, no
fallback, ranker-only latency at most 1.75 ms. For 100 synthetic candidates (the proposal's size) the
ranker-only p95 is about 3.5 ms with a budget and about 10 ms with no constraints, against the 25 ms target.

## Known limits

* `task_id` is optional in the request: the service does not pass a task id into the generic graph.
* The proposal's third identity rule, a normalised `(brand, model)` when there is no ASIN or provider id, is not
  implemented: the provider results carry no model field. Such an item keeps its title-and-link hash id.
* The live weights are the offline ones. There are no live relevance labels, so they cannot be re-tuned.
* No live check with a SerpApi key has been run.
* The repository returns at most 5,000 interactions, oldest first, so a user with more than that has their
  latest events cut off.

## What D4 does not show

The offline numbers (+0.81% NDCG@10 over B1) were measured on the Amazon Reviews dataset with a history query.
They do not transfer to live requests, which have a real query, a different popularity scale and no relevance
labels. For live requests the evidence is the 15 labelled searches: zero violators surfaced, the unknown-price
policy, the contract invariants and the feature-off equality with B0.

## Running it for the demo

```bash
# backend/.env or the shell, then restart the backend
METAREC_PRODUCT_RANKER=domain_v1     # or legacy for the "off" half of the demo
```

Run the same ProductRec query twice, once per setting. `python -m pytest tests/wp4 -q` runs the checks.
