# Member A Progress Tracker

Status of each milestone in [ROADMAP.md](ROADMAP.md). Update the table and
add a log entry (newest first) with every change on a Member A branch. Record only what was run and verified.

**Legend:** ✅ done · 🟡 in progress · ⬜ not started · ⛔ blocked

## Milestones

| # | Milestone | Deliverable | Target | Status | Branch | Evidence |
|---|---|---|---|---|---|---|
| M1 | Topology registry | D1 | 8 Nov | ✅ | merged to shared | `tests/test_wp8_topology.py` (26 tests) |
| M2 | Event contract (`GraphRunEventV1`) | D1 | 8 Nov | 🟡 | merged to shared | `tests/test_wp8_event_contract.py` (120 tests), `tests/test_wp8_golden_events.py` (40 tests); waiting on B for H1–H3, H7 and R1–R15 in `DECISIONS.md` |
| M3 | Single-invocation helper | D2 | 15 Nov | ✅ | `wp8/a-m3-invocation-helper` | `tests/test_wp8_invocation.py` (26 tests) |
| M4 | Runner plumbing + guard test | D2 | 22 Nov | ✅ | `wp8/a-m4-runner-plumbing` | `tests/test_wp8_integration.py` (12 tests), `tests/test_wp8_invocation.py` (27 tests) |
| M5 | Callback collector | D2 | 29 Nov | ✅ | `wp8/a-m5-callback-collector` | `tests/test_wp8_collector.py` (21 tests) |
| M6 | Task hand-off + trace completion | D2 | 6 Dec | ⬜ | — | — |
| M7 | WP5/WP7 adapter events | D2 | 13 Dec | ⬜ | — | — |
| M8 | Integration + capture evidence | D2–D3 | 24 Jan | ⬜ | — | needs B's projector and store |
| M9 | Trace client + polling hook | D4 | 20 Dec | ⬜ | — | — |
| M10 | DAG | D4 | 7 Feb | ⬜ | — | — |
| M11 | Timeline + selection sync | D4 | 21 Feb | ⬜ | — | — |
| M12 | Fidelity + overhead evidence | D6 (D5 phase) | 7 Mar | ⬜ | — | — |
| M13 | Handover | D6 | 21 Mar | ⬜ | — | — |

## Open items and decisions

Shared items now live in [../DECISIONS.md](../DECISIONS.md) (hand-offs
H1–H8, rules R1–R15, decided X1–X3). Member A's side of H4 and H5 is done;
H6 is planned for M4.

## Log

### 2026-11-03 — M5 callback collector

- **Branch:** `wp8/a-m5-callback-collector`, from `wp8/a-m4-runner-plumbing`
  at `9ac8123`.
- **Added:** `wp8_trace/collector.py` — `TraceCollector`
  (`run_inline`, so callbacks are handled in order on the caller's thread;
  `raise_error=False` plus an internal-error list, so a collector, sink or
  projector failure never reaches the run) and `new_trace_context()`.
  `wp8_trace/interfaces.py` — `DraftEvent` (`finalize()` builds the contract
  event), `AppendResult`, `Projection`, `SafeProjector` and `TraceEventSink`
  protocols (R16). `wp8_trace/stubs.py` — `NullProjector` (draft R10 error
  codes) and `InMemoryTraceSink` (validates every event, one lock for
  sequence + append).
- **Rules:** graphs by registered `run_name`; nodes by topology; routers and
  other inner runnables pass through to their node; native tool/LLM
  callbacks become metadata-only child spans of exactly their node; orphans,
  post-seal roots and `"LangGraph"`-named (untraced) graphs are withheld with
  their subtree and counted (R11, R12, R15); `CancelledError` → `cancelled`;
  errors reuse the run's `run_id`; injectable clock; one `graph_instance_id`
  per graph run with `open_instance`/`close_instance` on the sink;
  `seal()`, `on_untraced_graph()`, `open_run_ids`, `incomplete_reasons` for
  M6.
- **Harness:** E3 and E7–E11 accept an optional `trace_ctx` (default
  unchanged).
- **Tests:** `tests/test_wp8_collector.py`, 21 — live replays of nine golden
  cases (`full_debug_run`, both E3 turns, itinerary anchor, repeated
  domain graphs, caught and uncaught failure, cancellation, timeout) match
  event for event with parent structure; contiguous sequence, one root per
  turn, parents start first, each run closes once, no router nodes;
  instances link to their invoking node and close; fresh-context hand-off
  withheld without and linked with the explicit parent; no root after seal;
  unknown parent withheld with its subtree; untraced graph withheld and
  reported; pass-through and tool/LLM spans attach to the node; durations
  from the injected clock; failing sink or projector leaves the run intact;
  a seeded secret reaches the projector but no stored event, and prompts never
  reach the projector.
- **Verified:** full backend suite 856 passed, 14 skipped, in 3 repeated
  runs (+21 tests). Not yet: trace status/finalize and the explicit hand-off
  parent (M6), capture flags from B's config (M8), B's real projector/sink.

### 2026-11-03 — M4 runner plumbing + guard test

- **Branch:** `wp8/a-m4-runner-plumbing`, from `wp8/a-m3-invocation-helper`
  after the shared branch was merged into it.
- **Runners:** all five `ainvoke` sites now go through
  `invoke_with_wp8_trace` with a literal `graph_definition_id`;
  `run_routing_graph`, `run_generic_domain_graph`, `run_restaurant_graph`,
  `run_task_graph` and `run_request_orchestrator` take an optional
  keyword-only `trace_ctx`. `build_request_orchestrator_graph(..., trace_ctx=)`
  hands it to the routing node by closure.
- **Service (H6):** `handle_user_request_async(..., *, trace_ctx=None)` →
  `_handle_user_request_graph` → orchestrator, `create_task_adapter` →
  `create_task_async` → `_run_scoped_task` → `_run_task_graph_compatible`
  (only if the runner accepts it) → `run_recommendation_task_graph` →
  `run_task_graph` and both `_execute_*_domain_task`; the itinerary anchor
  adapter → `_execute_generic_domain_task`. Forwarded with
  `**trace_kwargs(ctx)` (new in `invocation.py`), which is `{}` while tracing
  is off, so untraced calls keep their exact arguments and existing test fakes
  with fixed signatures still work. The context never enters graph state or
  config.
- **Guard (R14):** `tests/test_wp8_integration.py` — AST scan of production
  code for graph-method attributes and `getattr` of them outside the helper;
  each registered ID at exactly one helper call site with a literal ID;
  five planted bypasses (direct, streaming, aliased, sync, getattr).
  Restoring a direct `graph.ainvoke` in `routing_graph.py` made both guard
  tests fail, naming `routing_graph.py:533`.
- **End to end:** a traced two-turn hotel debug run reaches orchestrator ×2
  (two roots, R11), routing (under `routing`), task (under
  `domain_dispatch`) and generic (under `domain_graph_dispatch`), none named
  `"LangGraph"`; the itinerary anchor's generic graph is under
  `domain_dispatch`; a traced restaurant task reaches the restaurant graph
  under `domain_graph_dispatch`. Tracing on vs off gives the same outcome and
  the same tool-adapter call count.
- **Probe harness:** `_confirm_chain`, `_hotel_chain` and
  `e4_itinerary_anchor` accept an optional `trace_ctx` (default unchanged).
- **Verified:** full backend suite 835 passed, 14 skipped, in 3 repeated
  runs (+13 tests). Not yet covered: the task hand-off parent across a fresh
  context and trace completion (M6), the collector itself (M5), and the debug
  route creating the context (H7, Member B).

### 2026-11-03 — Review fixes before M4 (shared branch + M3)

Review of M1–M3 against the brief found four defects and three plan gaps.

- **Shared branch (`WP8-LangGraph-Recommendation-Trace`):**
  - `models.py`: graph-level `error` must have `node_id: null`; numbers and
    booleans strict (`"17"`/`true` no longer coerced, matching the schema);
    `GraphInstance` IDs use the event safe-ID pattern and `ended_sequence`
    cannot precede `started_sequence`. Schemas regenerated (DECISIONS X2, X3).
  - `topology.py`: `builder_structure()` fast path for `matches_registered()`
    (~0.008 ms vs ~0.48 ms per traced call), falling back to full extraction
    when the builder differs or is unreadable.
  - Golden lists: new `full_debug_run` (E3 turns 1+2 in one trace, two turn
    roots); every manifest gains `source.root_turns` and `turn_roots`; the
    one-root check is now one root per turn (R11).
  - `DECISIONS.md`: R11 (one trace per debug run, finish condition), R12
    (orphans withheld and counted, trace `incomplete`; R5 unchanged), R13
    (what explicit propagation means), R14 (AST guard test scope), R15
    (untraceable graph runs once untraced), H6 (A: `trace_ctx` on
    `handle_user_request_async`), H7 (B: route passes and seals the trace),
    H8 (WP5/WP7 not in repo; M7 on fake events), X2, X3.
  - `ROADMAP.md` M2/M4–M8/M12, `MEMBER_A.md`, `MEMBER_B.md`, `FRAMEWORK.md`
    and the golden README updated to match.
- **M3 branch:** `invocation.py` no longer raises on an unregistered ID or a
  topology mismatch: it runs the graph once untraced, logs, and calls the
  collector's optional `on_untraced_graph()` (R15); `TopologyMismatchError`
  removed. An explicit hand-off parent is now applied to a callback manager
  and when the collector is already present (it was silently dropped in
  both). Tests: 26 (+9) — hand-off parent for five callback forms, copied
  context with hand-off, untraceable graphs run once and are reported, a
  collector without or with a failing report hook does not break the run.
- **Verified:** shared branch alone 796 passed, 14 skipped; M3 branch alone
  805 passed, 14 skipped; both combined (the state after merging the shared
  branch into M3) 822 passed, 14 skipped, of which 224 are WP8 tests. The
  two change sets apply together with no conflicts.

### 2026-10-29 — M3 single-invocation helper

- **Added:** `langgraph_metarec/wp8_trace/invocation.py` — `TraceContext`
  (frozen; `handoff(parent_run_id)` for boundaries that lose context),
  `traced_config()`, `invoke_with_wp8_trace()` and `TopologyMismatchError`;
  exported from `wp8_trace`.
- **Design:** with tracing off the call is the runner's old call, config
  object and all. With tracing on, only `run_name` and (if not inherited)
  `callbacks` change. Checked first: LangChain's `ensure_config` replaces any
  explicitly passed key wholesale, so the helper never passes `metadata` or
  `configurable`; `run_name` was confirmed not to reach checkpoint metadata.
  Topology verification costs ~0.4 ms per traced call (graph build is ~7 ms).
- **Tests:** `tests/test_wp8_invocation.py`, 17 `backend_unit` tests — plain
  call when off/disabled; config merge keeps business keys and does not
  mutate the caller's config, list or manager; no duplicate collector;
  the real routing graph traced is named `routing-graph@v1` with identical
  output; a nested call keeps the inherited node parent and checkpoint
  namespace and the same run tree as an untraced call, and `run_name` does
  not leak; each callback received once; an explicit `handoff` parent links
  a graph across a fresh `contextvars.Context()` (the E6b case), without it
  the graph is orphaned; side-effect counter = 1 for off/on × ok/error/cancel
  with the collector seeing `RuntimeError`/`CancelledError`; unknown or
  mismatched IDs refused before running; checkpoints identical traced vs not.
- **Verified:** 17 passed in 6 repeated runs; full backend suite 796 passed,
  14 skipped. No production module changed yet (M4 wires the runners).

### 2026-10-29 — Branch workflow; M1–M2 merged to the shared branch

- Merged M1, M2, the golden event lists and the decisions into
  `WP8-LangGraph-Recommendation-Trace` and pushed it to `MetaRecTeam` and the
  fork. Created `wp8/a-m3-invocation-helper` from it for M3.
- Added the branch workflow to `team/README.md` §7.

### 2026-10-29 — M2: golden event lists, schema matrix, shared decisions

- **Dependency:** `jsonschema==4.26.0` added to `requirements-dev.txt`, the
  `pyproject.toml` dev group and `uv.lock` (adds `jsonschema`,
  `jsonschema-specifications`, `referencing`, `rpds-py`, `attrs`).
- **Schemas:** the checked-in event schema now encodes the required/null
  matrix as 2020-12 `if/then` rules, the safe-value caps as depth-limited
  `$defs` (via `contract_json_schema()`), and UTC-only timestamps. Every
  broken variant the model rejects is rejected by the schema alone, except
  non-finite numbers (JSON cannot carry them).
- **Golden event lists:** `scripts/wp8_golden_events.py` converts the real D0
  recordings into 10 cases in `tests/fixtures/wp8/golden/` (8 real turns, 2
  marked synthetic), each with a manifest; routers dropped, graphs named from
  the registry, failures/cancellations as one `error` event per run.
- **Tests:** +52 contract tests (schema validity, every fixture through the
  schema, every broken variant rejected by the schema); +36 golden tests
  (reproducible from D0 data, model + schema valid, one root, parents start
  first, one terminal event per run, no router nodes, manifests match,
  task outlives request, error order, repeated instances, synthetic labels).
- **Docs:** `docs/wp8/team/DECISIONS.md` (hand-offs H1–H5, rules R1–R10,
  decided X1); "Waiting on you now" section in `MEMBER_B.md`; golden README.
- **Verified:** full backend suite 779 passed, 14 skipped.

### 2026-10-29 — M2 event contract (backend half)

- **Added to `wp8_trace/models.py`:** enums for `event_type`, `status`,
  `runnable_type`; `GraphRunEventV1` with all 23 keys required (nullable,
  never omitted) and unknown fields rejected; sub-shapes `PayloadSummary`,
  `StateDiff` (`ChangedValue`, `Omission`), `EventMetadata` (`LlmAttempt`,
  `ToolScope`, `ToolHealth`), `SafeError`, `RedactionInfo`; the safe-value
  grammar with the hard caps (512 chars, 50 items, 6 levels, finite numbers);
  UTC-only timestamps; bounded opaque IDs.
- **Null matrix:** a model validator enforces the brief's per-event rules —
  allowed `runnable_type`s, required `graph_instance_id`/`run_id`, node and
  child-span parents, graph lifecycle without `node_id`, error events carry
  `error` and status `error|cancelled`, `trace_truncated` without run IDs,
  starts are `running` with null duration, ends are not `running`.
- **Schemas:** `contracts/wp8/{graph-run-event,graph-definition,graph-instance}-v1.schema.json`
  (2020-12) from `scripts/wp8_export_schemas.py`; a test fails if they go stale.
- **Fixtures:** `tests/fixtures/wp8/events/valid_events.json` — one valid
  event per matrix row (root and nested graph, node, tool with health, WP5
  attempt, WP7 scope, cancelled node error, root graph error, truncation).
- **Tests:** `tests/test_wp8_event_contract.py`, 56 `backend_unit` tests: the
  brief's own example event, 11 valid fixtures round-trip with every key
  present, 40 broken variants rejected, cap boundaries, schema parity.
- **Verified:** full backend suite 691 passed, 14 skipped. Still pending for
  M2: golden trace fixtures and TypeScript parity (both with B).

### 2026-10-29 — M1 topology registry

- **Branch:** created `wp8/member-a-capture` from `WP8-LangGraph-Recommendation-Trace` at `413cc94`.
- **Added:** `langgraph_metarec/wp8_trace/` package — `models.py` (strict
  Pydantic v2 `TopologyNode`, `TopologyEdge`, `GraphDefinition`,
  `GraphInstance`), `topology.py` (extracts definitions from the compiled
  graphs, labels conditional edges with their route key, records edge-router
  names, fingerprints each definition, `matches_registered()`).
- **Registered:** `request-orchestrator@v1`, `routing-graph@v1`,
  `task-graph@v1`, `generic-domain@v1`, `restaurant-domain@v1`.
- **Snapshots:** `tests/fixtures/wp8/topology/*.json`, regenerated by
  `scripts/wp8_topology_snapshot.py`.
- **Tests:** `tests/test_wp8_topology.py`, 25 `backend_unit` tests — snapshot
  match, node IDs equal `GRAPH_EXECUTION_FLOW.md` §4, well-formedness,
  routing's 7 conditional edges, router names (`_route_after_collect_confirm`,
  `route_after_classification`, `route_after_retry`) are not nodes,
  adapter-independence, drift detection, strict models.
- **Verified:** 25 passed; full backend suite 635 passed, 14 skipped;
  deliberately editing a snapshot makes the snapshot test fail with the bump
  hint; no production file changed.
