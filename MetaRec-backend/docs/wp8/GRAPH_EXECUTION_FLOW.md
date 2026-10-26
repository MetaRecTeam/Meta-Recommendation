# MetaRec Graph Execution Flow: Parentage and Callback Order

**Purpose:** a verified, code-grounded map of exactly how the five compiled
MetaRec LangGraph graphs call each other, in what order, and which of those
calls a LangChain/LangGraph callback collector will actually see versus which
are raw Python calls that WP8 can only observe through the WP5/WP7 typed
adapter sink. It grounds the WP8 proposal's contracts (`WP8_README.md`, kept
with the project's planning documents rather than in this repo) in the exact
call sites, so a collector implementer does not have to re-derive this map.
New to WP8? Start with [README.md](README.md) for the vocabulary.

**Verified against:** branch `WP8-LangGraph-Recommendation-Trace`, commit
`9c09346`. Every claim below cites a file and function name that was read
directly, and §3 was additionally checked at runtime
([parentage-baseline/REPORT.md](parentage-baseline/REPORT.md)). Re-verify
against your own pinned commit before relying on line numbers.

**Diagrams** — `.mmd` source plus a rendered `.png` in [diagrams/](diagrams/);
regenerate the PNG from the source if they drift:

| Graph | Diagram |
|---|---|
| `request_orchestrator.py` | [request_orchestrator.png](diagrams/request_orchestrator.png) |
| `routing_graph.py` | [routing_graph.png](diagrams/routing_graph.png) |
| `task_graph.py` | [task_graph.png](diagrams/task_graph.png) |
| `generic_graph.py` | [generic_graph.png](diagrams/generic_graph.png) — shows both of its real callers (§3) |
| `restaurant_graph.py` | [restaurant_graph.png](diagrams/restaurant_graph.png) |
| One request end to end, as `run_id`/`parent_run_id` | [callback_sequence.png](diagrams/callback_sequence.png) — idealised; the measured version is in the baseline report's figures |

## 1. The five graphs and their entry points

There are exactly five direct production `.ainvoke(...)` call sites in the
repository (confirmed by `grep -rn "\.ainvoke(" langgraph_metarec service.py
internal`; no `astream`/`astream_events`/`stream_events` call exists anywhere
in the audited code):

| # | Graph | Built by | Invoked by (`run_*` function) | `ainvoke` call site | Config passed today |
|---|---|---|---|---|---|
| 1 | `request_orchestrator.py` | `build_request_orchestrator_graph` | `run_request_orchestrator` | `request_orchestrator.py:1721` | `{"configurable": {"thread_id": conversation_thread_id(...)}}` — **merge**, do not replace |
| 2 | `task_graph.py` | `build_task_graph` | `run_task_graph` | `task_graph.py:188` | `{"configurable": {"thread_id": task_thread_id(...)}}` — **merge**, do not replace |
| 3 | `routing_graph.py` | `build_routing_graph` | `run_routing_graph` | `routing_graph.py:531` | none — second `ainvoke` argument is omitted entirely |
| 4 | `generic_graph.py` | `build_generic_domain_graph` | `run_generic_domain_graph` | `generic_graph.py:962` | none |
| 5 | `restaurant_graph.py` | `build_restaurant_graph` | `run_restaurant_graph` | `restaurant_graph.py:629` | none |

Root callers, confirmed by call-site grep:

- `run_request_orchestrator` ← `service.py:4073`, inside `_handle_user_request_graph` (the public chat-message path).
- `run_task_graph` ← `service.py:3160`, inside `run_recommendation_task_graph` (the background-task path).
- `run_routing_graph` ← `request_orchestrator.py:1332`, inside `routing_node` (a node of graph #1) — **only** call site outside tests.
- `run_generic_domain_graph` ← `service.py:1974`, its only direct call site, inside `_execute_generic_domain_task`. That wrapper has two production callers: the task's `run_domain` closure (`execute_domain_task`), and `service.py:4061` inside `resolve_itinerary_anchor_adapter`, which `request_orchestrator.py`'s `domain_dispatch_node` reaches via `_resolve_itinerary_anchor_preferences`. Two production paths with two distinct parentage shapes — see §3.
- `run_restaurant_graph` ← `service.py:1828` only (`_execute_restaurant_domain_task`, called from the task's `run_domain` closure). One production call site.

## 2. Per-graph node topology

### 2.1 `request_orchestrator.py` — `RequestOrchestratorState`

```text
START -> intention -> collect_confirm --[intent in {query, confirmation_yes}]--> routing -> domain_dispatch -> result -> END
                                       --[otherwise]-----------------------------------------------------------> result
```

| Node | What it does | External calls made from inside the node |
|---|---|---|
| `intention` | Classifies intent/preferences for the turn | `adapters.analyze_message` → `llm_service.analyze_user_message` (raw SDK call) |
| `collect_confirm` | Applies HITL confirm/reject/refine logic; decides whether to route or reply directly | none |
| `routing` | Classifies the domain and mode | `run_routing_graph` (nested `ainvoke`, **real node parent**) |
| `domain_dispatch` | Builds the confirmation message, resolves itinerary anchors, or creates the background task | `adapters.make_confirmation` (raw SDK call); `_resolve_itinerary_anchor_preferences` → `run_generic_domain_graph` (nested `ainvoke`, **real node parent**, itinerary only); `adapters.create_task` → `service.create_task_async` → detached `asyncio.create_task` (callback parent survives via contextvar copy, but the task **outlives** this node — see §3) |
| `result` | Assembles the final response payload | none |

### 2.2 `routing_graph.py` — `RoutingRuntimeState`

```text
START -> domain_classification --[domain==unknown]--> retry_domain --[mode==domain_error]--> domain_error -> END
                                --[mode==itinerary]--> itinerary -> END
                                --[mode==multi_domain]--> multi_domain -> END
                                --[else]--> single_domain -> END
                    retry_domain --[mode==multi_domain]--> multi_domain -> END
                    retry_domain --[else]--> single_domain -> END
```

All six node functions (`domain_classification`, `retry_domain`,
`single_domain`, `multi_domain`, `itinerary`, `domain_error`) are plain `def`,
not `async def` — no I/O, no nested calls. This graph is pure classification
logic; every `node_start`/`node_end` pair should be near-instantaneous, which
is a useful fixture property (a "fast, synchronous, deterministic" branch
fixture for WP8's fidelity tests).

### 2.3 `task_graph.py` — `TaskGraphState`

```text
START -> task_started -> domain_graph_dispatch -> result_projection -> END
```

| Node | What it does | External calls made from inside the node |
|---|---|---|
| `task_started` | Writes the initial `TaskStatusProjection` | `adapters.write_projection` |
| `domain_graph_dispatch` | Runs the domain graph(s) for this task; for an itinerary task this loops over multiple domains (attraction, then restaurant/hotel as needed) sequentially inside the same node call | `adapters.run_domain_graph(progress_callback)` → `service.run_domain` → `execute_domain_task` → `run_generic_domain_graph` and/or `run_restaurant_graph` (nested `ainvoke`(s), **real node parent** — see §3) |
| `result_projection` | Pass-through; the terminal projection was already written by `domain_graph_dispatch` | none |

### 2.4 `generic_graph.py` — `GenericRuntimeState`

```text
START -> candidate_gather -> normalize_and_rank -> recommendation_result -> END
```

`candidate_gather` calls `registry.dispatch(tool_name, params, ...)` wrapped in
`asyncio.to_thread(...)`, and optionally an LLM "gather reasoner"
(`propose_gather_action`, a raw `llm_service` call embedded in a prompt, not
native function/tool calling). **Neither is a LangChain `Runnable`**, so
neither fires a native `on_tool_start`/`on_tool_end` or
`on_llm_start`/`on_llm_end` callback — WP8 can only see them through the
WP5 (`llm_attempt_adapter`) / WP7 (`tool_scope_adapter`, plain `tool_start`)
typed-event sink described in `WP8_README.md` §"Capture and persistence
design", not through generic callback instrumentation.

### 2.5 `restaurant_graph.py` — `RestaurantRuntimeState`

```text
START -> candidate_gather -> rerank_and_summarize -> validation_and_calibration -> recommendation_result -> END
```

`candidate_gather` calls the restaurant planner (`agent/agent_plan.py`, the
**one** place in the codebase that does real native tool-calling against the
Chat Completions API) via `asyncio.to_thread(planner, client, ...)`, then
dispatches the planner's chosen tools via
`asyncio.to_thread(registry.dispatch, ...)`. Same consequence as §2.4: both
are raw synchronous/threaded calls, not LangChain runnables, so both need the
WP5/WP7 adapter sink rather than native callbacks.

## 3. Parentage: which nested `ainvoke` gets a real node parent

**None of the five graphs are LangGraph subgraphs.** Each is a separately
compiled `StateGraph` that ordinary Python code calls; none is registered as
a node inside another via `subgraphs=True` or dotted namespaces. Parentage
must come from the callback `run_id`/`parent_run_id` chain plus whatever
config WP8 propagates explicitly — never from graph identity or domain name.

That said, "not a subgraph" does not mean "not a node's child at the callback
level." LangChain seeds every config from the `var_child_runnable_config`
contextvar (`langchain_core.runnables.config.ensure_config`), and an explicit
config only overrides the keys it sets — so a nested `ainvoke` inherits the
enclosing node's callbacks whenever that contextvar reaches it.

**Every row below was runtime-verified on 2026-10-04** with a passive callback
listener over the real graphs and existing test fixtures
([parentage-baseline](parentage-baseline/REPORT.md)). An earlier
version of this table, written from code reading alone, said the
`task_graph` row was a parentage break with `parent_run_id = null`. That was
wrong; the corrected row and the reason are below.

| Nested graph | Called from | Observed callback parent | Probe | Notes |
|---|---|---|---|---|
| `routing_graph` | `request_orchestrator.py` `routing_node` | `routing` node run | E3, E4, E5 | No config passed; parent inherited via contextvar. LangGraph also namespaces it like a subgraph (`checkpoint_ns = routing:<id>`). |
| `generic_graph` (itinerary anchor resolution) | `request_orchestrator.py` `domain_dispatch_node` via `_resolve_itinerary_anchor_preferences` → `resolve_itinerary_anchor_adapter` | `domain_dispatch` node run | E4 | Runs **before any task exists**; the older docs' blanket "no node parent" framing missed this call site. |
| `generic_graph` / `restaurant_graph` (task domain dispatch) | `service.py` `run_domain` → `execute_domain_task` → `_execute_generic_domain_task` / `_execute_restaurant_domain_task` | `domain_graph_dispatch` node run (of `task_graph`) | E3, E5, E11 | Namespaced like a subgraph: graph `checkpoint_ns = domain_graph_dispatch:<id>`, so its nodes run under `domain_graph_dispatch:<id>\|candidate_gather:<id>` and so on. |
| `task_graph` itself | `request_orchestrator.py` `domain_dispatch_node` (`confirmation_yes`) via `adapters.create_task` → `service.create_task_async` → `asyncio.create_task` | `domain_dispatch` node run — **chain intact** | E3, E5 (10/10), E6a | `asyncio.create_task` copies contextvars, so the callback parent survives. But: **(a)** the child outlives its parent — the request graph emits `graph_end` while `task_graph` is still running; **(b)** the link is implicit only — E6b, the identical hand-off with a fresh `contextvars.Context()`, yields `parent_run_id = null`; **(c)** callback metadata on task runs carries the *conversation* `thread_id` and parent `checkpoint_ns`, not the task's own; **(d)** because `run_task_graph` passes an explicit `configurable`, LangGraph gives it its **own root checkpoint namespace** — so callback tree (child) and checkpoint namespace (root) disagree for this one graph. Invariant 2 still applies: WP8 must propagate trace context explicitly so the link does not depend on the contextvar surviving. |

**Other runtime facts the collector must handle (same probe):**

- **No graph identity in callback data.** All five compiled graphs report run
  name `"LangGraph"`. The probe had to infer which graph ran from its first
  node. WP8 must name graphs itself (e.g. `compile(name=...)` or
  `with_config(run_name=...)`) or carry `graph_definition_id` in its own
  context — reviewed plumbing, not a business change.
- **Conditional-edge routers appear as child runs of nodes**
  (`_route_after_collect_confirm` under `collect_confirm`,
  `route_after_classification` under `domain_classification`). They are not
  topology nodes and must not be counted as node visits.
- **Zero tool or LLM callback events** in every experiment, even though tool
  adapters and the fake LLM client were called — confirming §2.4–§2.5.
- **No behaviour change from listening.** With the listener on vs off, the
  hotel chain made the same 3 tool-adapter calls and returned identical items
  (n=1 — not yet the README's 100-pair benchmark).
- **Failure, cancellation and timeout (E7–E10).** Every open run closes with
  `chain_error` on the same `run_id`, innermost first; no run was left without
  an end. Cancellation and timeout arrive as `chain_error(CancelledError)`, so
  the collector must map the exception class to `cancelled`. An exception the
  service catches (`_execute_generic_domain_task`'s `try/except`) is visible on
  `generic_graph` but every ancestor — and the task's business status — reports
  success.
- **Repeated instances (E11).** A multi-domain request runs two
  `generic_graph` instances under the same `domain_graph_dispatch` run,
  distinguishable only by `run_id`.

All of the above is asserted by
[tests/test_wp8_parentage_baseline.py](../../tests/test_wp8_parentage_baseline.py)
(12 `runtime_contract` tests); analysis in
[parentage-baseline/REPORT.md](parentage-baseline/REPORT.md).

**Practical rule for the collector:** take parentage from the observed
`parent_run_id`, never from graph identity, and treat "parent present" and
"parent still open" as separate questions. All four nested call sites above
have a callback parent today; one of them (`task_graph`) gets it only because
`asyncio.create_task` happened to copy the contextvar, and it routinely
outlives that parent. Fixture coverage must therefore include both the
inherited and the fresh-context hand-off (E6a/E6b), or a future refactor of
`create_task_async` will orphan task traces without any test failing.

## 4. Topology IDs: suggested `graph_definition_id` / `node_id` mapping

For §1's `GraphTraceV1.topology.definitions`, the natural stable IDs (matching
each compiled `StateGraph`'s own node names, so `node_id` needs no
translation layer):

| `graph_definition_id` | Source | `node_id`s (in topological order) |
|---|---|---|
| `request-orchestrator@v1` | `request_orchestrator.py` | `intention`, `collect_confirm`, `routing`, `domain_dispatch`, `result` |
| `routing-graph@v1` | `routing_graph.py` | `domain_classification`, `retry_domain`, `single_domain`, `multi_domain`, `itinerary`, `domain_error` |
| `task-graph@v1` | `task_graph.py` | `task_started`, `domain_graph_dispatch`, `result_projection` |
| `generic-domain@v1` | `generic_graph.py` | `candidate_gather`, `normalize_and_rank`, `recommendation_result` |
| `restaurant-domain@v1` | `restaurant_graph.py` | `candidate_gather`, `rerank_and_summarize`, `validation_and_calibration`, `recommendation_result` |

Bump the `@v1` suffix whenever a node is added/removed/renamed in the
corresponding file — this list only reflects the pinned commit above.

## 5. Where this differs from the WP8 proposal

The proposal's invocation inventory, config-merge table, `DebugTraceStorage`
description, feature flags and 1.5 s Debug Arena polling all match this
commit. Three of its statements do not:

1. **"`restaurant_graph` and `generic_graph` are siblings beneath the task,
   not children of any node run."** Both are children of a node run at every
   production call site (§3), including the itinerary-anchor call that runs
   before any task exists.
2. **"LangGraph's subgraph machinery will not link them; looking for it is a
   dead end."** The no-config nested graphs inherit the calling node's config
   and get subgraph-style checkpoint namespaces at runtime (§3). Whether
   `subgraphs=True` streaming would surface them is untested.
3. **Parent links at the background-task hand-off.** Not stated in the
   proposal, but our own first reading of the code concluded the chain breaks
   at `asyncio.create_task`. It does not (§3); it survives implicitly, and the
   child outlives its parent.

## 6. Reading order for a new contributor

1. [README.md](README.md) for the vocabulary.
2. §1–§3 of this document with the [diagrams](diagrams/).
3. [parentage-baseline/REPORT.md](parentage-baseline/REPORT.md) for what the
   running system actually does, including failure and cancellation.
4. Open the five graph files in the order of §1's table and confirm the line
   numbers still match your pinned commit (they move; the symbol names and
   call shapes should not).
