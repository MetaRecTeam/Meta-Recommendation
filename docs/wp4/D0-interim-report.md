# WP4 · D0 — Interim Report

Ong Chuan Hau (C240087) · MetaRec `feat/wp4-constraint-aware-product` · base commit `9c09346`

D0 scope (proposal): *trace router → product tool parameters → providers/enrichment
→ normalisation/deduplication → generic rank → strict API/UI; reproduce B0; audit
item feedback, IDs, prices and missing fields; freeze contracts, dataset/category,
split and constraint fixtures.*

---

## 1. Pipeline trace (a product request, end to end)

| # | stage | code | what happens for `domain = product` |
|---|---|---|---|
| 1 | HTTP | `main.py` `POST /api/process` | one endpoint: intent analysis, preference extraction, HITL confirm, routing, task creation |
| 2 | intent + preferences | `service.py` (LLM) | free-text query → structured `preferences`; user confirms before search (`hitl_state`) |
| 3 | routing | `langgraph_metarec/graphs/routing_graph.py` `run_routing_graph` | product route chosen by keyword classification **or** a preference frame carrying `brand`/`category` (`_DOMAIN_ENTITY_KEYS["product"]`). Emits `DomainRoute(domain="product", execution_domain="product", status="ready", tool_tags=["#thing","#shopping","#product"])` |
| 4 | task dispatch | `langgraph_metarec/graphs/task_graph.py` → `service.py` `_execute_generic_domain_task` | preferences fused as `{**profile_domain_slice, **request_preferences}` |
| 5 | product preference normalisation | `request_orchestrator.py` `_normalize_product_preferences` | regex fills `product`, `brand`, `category`, `use_case`, `model`, `budget`. **`budget` is a text string** (`"<= 120 USD"`, `"120 USD"`, `"affordable"`). No structured budget, no required attributes, no brand exclusions. |
| 6 | candidate gather | `generic_graph.py` `candidate_gather` | `ToolRegistry.resolve(domain="product", tags=[...])` → `amazon.product.search` (active only if `SERPAPI_KEY` set). Seed pass: `_parameters_for_tool` → `_product_search_query(query, prefs)` concatenates the query with budget-text / category / brand / use_case / model tokens into **one search string** → `{"query": <string>, "max_results": 10}`. ReAct refine loop: `amazon.product.search` is not in `_RELAX_ORDER`, so no relaxation happens — the loop just stops. |
| 7 | provider | `tool_registry.py` `_amazon_product_search_adapter` | SerpApi `engine=amazon`, `k=<query string>`. From each `organic_results[:10]` keeps **only** `{title, brand, link, rating, reviews, price, thumbnail, source}`. Then `compact_tool_output` caps free text. |
| 8 | enrichment | — | **none for products.** (Places get gmap/osm merge; products do not.) |
| 9 | normalisation | `generic_graph.py` `normalize_tool_items` (amazon branch) → `_item()` | `title`←title · `subtitle`←`brand or price` · `image_url`←thumbnail · `url`←link · `rating` · `reviews_count`←reviews · `source`="Amazon" · `tags`←`[brand?, price?]` · `why`="Matched the product search query." · `id` ← `raw.product_id or raw.asin` **→ always empty** (step 7 drops both) → falls back to `"product_" + sha1(tool\|title\|url)[:16]` |
| 10 | dedup | `generic_graph.py` `_rank_items` → `_same_recommendation` | non-place items merge when canonical title matches **and** (same `url`, or — if neither has a url — same canonical `subtitle`). Merge keeps the higher-scored copy. |
| 11 | **generic rank (B0)** | `generic_graph.py` `_item_score` | sort key = `(rating or 0, reviews_count or 0, raw.popularity or 0, title)`, descending. **No price, budget, brand, or category term anywhere.** `[:10]`. |
| 11a | *(WP4 M2 gate)* | `generic_graph.py` + `product_constraints.py` | hooked here, **behind `METAREC_PRODUCT_RANKER` (default `legacy` = no-op)** |
| 12 | result object | `service.py` `_execute_generic_domain_task` | `RecommendationResult(items=[RecommendationItem(**item) …], confidence_score=0.85 if items else 0.45)` |
| 13 | strict API / UI | `RecommendationItemAPI` (`StrictBaseModel`) | exposes `id, domain, title, subtitle, description, image_url, url, rating, reviews_count, source, tags, why, gps_coordinates`. The internal `raw` payload is **deliberately stripped** (`_client_safe_item`). |
| 14 | persistence + feedback | `_persist_recommendation_result` → `result_id`; `internal/item_interactions/` behind `POST /api/item-interactions` | logs `(user_id, domain, item_id, action)`; projected to `ItemInteractionV1` |

### B0 definition (frozen)

**B0 = `_rank_items(normalize_tool_items("amazon.product.search", <serpapi output>, "product"))[:10]`** — steps 9–11 with the M2 flag off. Nothing between the provider and the strict API touches price or any stated constraint.

### Where the result reaches the screen (frontend)

- `MetaRec-ui/src/ui/Chat.tsx`, `GenericItemsSection` renders items **in the order received**. There is no client-side sorting, so the ranker's order is exactly what the user sees and a ranking change needs no UI change.
- Each card shows the image, title, domain badge, `subtitle`, rating, review count, source, up to 8 `tags`, `description`, `why`, and a "View source" link (`url`).
- **There is no price field on the card.** For products, price reaches the screen only as text: as the `subtitle` when the brand is empty (91 % of candidates) and again as a tag chip. The strict API has no numeric price or currency field, and the proposal adds none.
- The `why` line is the only free-text explanation slot. Today it always reads "Matched the product search query."
- The Save / Not interested / Purchased chips (`ItemInteractionControls.tsx`) appear for registered users only. Each chip posts `item_id = item.id`, the title-hashed id from section 3.1, plus a small snapshot (`title`, `subtitle`, `source`, `url`).
- On load, each card fetches existing interactions for the ids on screen (`listItemInteractions`, keyed by item id). By code reading, if a product's id changes between searches its earlier "Saved" state would not show. This has not been observed live.
- Because the snapshot stores `url` and the ASIN sits inside the url, stored interactions could be re-keyed to ASIN offline if the id scheme changes.

---

## 2. B0 reproduction

`python -m tests.wp4.eval_constraints --baseline` ranks the frozen candidate pools with `_rank_items` directly (no network) and counts labelled violators in the top-10.

| metric | B0 |
|---|---|
| labelled hard-constraint violators surfaced | **52 / 52 (100 %)** |
| cases with a violator at rank 1 | 6 / 15 |
| cases with a violator in the top 3 | 9 / 15 |

**How to read the 100 %.** It is true by construction and should not be quoted as a finding. Each pool holds at most 10 products, B0 shows the top 10, and B0 removes nothing, so every violating product necessarily reaches the user. The evidence that matters is different: 52 of the 146 products (36 %) that the provider returned break the user's own stated constraints, and in 9 of 15 queries a violating product ranks in the top 3 (rank 1 in 6). The labels behind these counts were assigned by applying each query's stated constraints to the returned titles and prices; they are a judgment and should be spot-checked against the case files in `tests/wp4/cases/`.

Cause: `_item_score` rewards rating + review count, so a well-reviewed item that is 55 % over budget (or the wrong category, or an excluded brand) outranks cheaper compliant items. This is the exact failure the proposal's acceptance criterion 2 targets.

B0 is also frozen at the **strict-API level**, which is what acceptance criterion 1 compares against. `tests/wp4/golden/*.json` holds, for each of the 15 cases, the exact items a client would receive today: the internal `raw` payload removed the way `main._client_safe_item` does it, and every item validated against `RecommendationItemAPI`. `tests/wp4/test_b0_golden.py` checks that with the ranker flag unset or `legacy` the output equals those files, and that switching the ranker on changes at least one of them, so the fixtures are able to detect a change. Regenerate them only if B0 is meant to change: `python -m tests.wp4.freeze_b0`.

---

## 3. Audit

### 3.1 IDs: derived from title and link, and the title changes between runs

The strict-API `id` for a product is `"product_" + sha1("amazon.product.search|" + title + "|" + link)[:16]`.

- The SerpApi Amazon result carries an ASIN, but `_amazon_product_search_adapter` does not keep it, so `raw.product_id` / `raw.asin` are always absent and the hash is always used.
- **The ASIN is recoverable anyway.** All 140 captured links contain `/dp/<ASIN>` (`python -m tests.wp4.audit_candidates`).
- **Measured stability** (`python -m tests.wp4.recheck_stability`, three queries re-run live on 18 Sep 2026, 12 products present in both runs): the link never changed (0 of 12), but the title changed for 4 of 12 (33 %). Because the id hashes the title, about one product in three would get a different id on a repeat search. An ASIN-based id would have been identical for 12 of 12.
- The item-interaction seam keys on this `item_id`, so an item a user saved or hid could fail to match on a later search. That follows from the measurement above; it has not been observed end to end.
- The sample is small (3 queries), so read 33 % as an indication rather than a rate.
- **D1 action:** derive the ASIN from the link (or keep it from the provider) and use it as `item_id`, with the hash only as a fallback.

### 3.2 Prices: display strings with a currency symbol, 2 % missing, and they move between runs

- Present for 137 of 140 candidates (98 %); the 3 missing come from two queries. All 137 parse to a number and all use the `$` symbol. No ranges (`"$a - $b"`) appeared in this sample, although `product_constraints.parse_price` handles them.
- There is no currency field, so currency is inferred from the symbol. `$` is ambiguous: the app treats `$` as SGD elsewhere, while SerpApi Amazon returns USD.
- **Prices move between runs.** Of the 12 products seen in both runs, the price changed for 7 (58 %), for example $118.99 to $139.99 and $49.99 to $39.99. A budget constraint could pass on one run and fail on the next, so evaluation has to run on frozen snapshots rather than live calls.
- A policy is still needed for the missing 2 %. The prototype gate currently treats an unknown price as "not a budget violation".

### 3.3 Missing and unreliable fields (measured over 140 live candidates)

| field | present |
|---|---|
| `title`, `rating`, `reviews`, `link`, `thumbnail` | 140 / 140 (100 %) |
| `price` | 137 / 140 (98 %) |
| `brand` | 12 / 140 (9 %). Populated in only 2 of 14 queries, the two whose query named a brand (Sony, Logitech); empty for all others |
| category / `type` | never. No candidate carries such a field |
| `asin` | not returned as a field, but present in the link for 140 / 140 |

Two further findings from the live re-run. Only 12 of 30 items (40 %) appeared in both the frozen and the re-run result for the same query, and the relative order of the shared items differed in all three queries. Live retrieval is therefore not repeatable, which is why B0 has to be reproduced from frozen candidate snapshots (acceptance criterion 1 requires no network).

Category and brand constraints can therefore only be checked against the title today. D1 should evaluate the Amazon Reviews 2023 metadata as a source of category and brand.

### 3.4 Item feedback seam

- `POST /api/item-interactions` (`internal/item_interactions/`), auth-session required, guests rejected in-endpoint.
- Actions: `save`, `hide` (stateful toggles, one active row per `(user, domain, item)`; save un-hides, hide un-saves), `positive`, `negative`, `consumed` (append-only). Product `consumed` label = "Purchased".
- Wire shape `ItemInteractionV1 = {schema_version:"item-interaction.v1", event_id, domain, item_id, action, result_id, occurred_at}` — docstring: *"consumed by the domain rankers and the offline evaluators"*. This is the input for M2's authenticated-user affinity feature.
- Keyed on `(user_id, domain, item_id)` — see the ID-stability caveat in 3.1.

---

## 4. Freeze list

| artefact | status | location |
|---|---|---|
| B0 definition | **frozen** (§1) | `generic_graph._rank_items` @ `9c09346` |
| Strict item contract | **frozen** | `RecommendationItemAPI` (13 fields, no `raw`) |
| OpenAPI contract | **frozen** | `contracts/metarec-openapi.json` |
| Item-interaction contract | **frozen** | `ItemInteractionV1` (`business_models.to_interaction_v1`) |
| Generic-graph metadata shape | **frozen** (+`constraint_filter`) | `generic_graph.recommendation_result` |
| Constraint fixtures | **frozen — 15 cases** | `tests/wp4/cases/*.json` (12 constraint queries + 3 clean controls, 52 labelled violators; case_01 was transcribed from a log, the other 14 are live SerpApi captures) |
| B0 strict-API output | **frozen, 15 golden files** | `tests/wp4/golden/` |
| Ranking dataset (Amazon Reviews 2023 subset) | **not selected**, needs a supervisor decision | — |
| Split (chronological per the proposal) | **cutoff date not set**, needs a supervisor decision | — |
| Graded relevance judgements | **not started** — needed for NDCG@10 (D5) | — |

---

## 5. Status

- **Done:** server-side and frontend pipeline trace; B0 reproduced offline from frozen candidates and frozen at the strict-API level (15 golden files); field, ID and price audit measured (section 3); 15 constraint fixtures frozen.
- **Still open, and needs the supervisor:** which Amazon Reviews 2023 category and subset size to use; the chronological split cutoff; the statistical thresholds (bootstrap minimum effect, complex-query margin); the evaluation currency; the missing-price policy; whether the ASIN change counts as in scope.
- **Corrections to the first draft of this report:** missing prices are 2 %, not "about 10 %"; no price ranges were observed; the "observed ID drift" was unsupported when first written and is now measured (section 3.1); brand is populated for 9 % of candidates and only when the query names a brand.
- A prototype of the hard-constraint gate exists behind the flag (`product_constraints.py`). It is not part of D0.
