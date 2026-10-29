# Member A Roadmap — Capture → Live Explorer

Milestones in build order. Each one ends with something testable that the
next one builds on, so the branch is never left with half a feature. Status
lives in [PROGRESS.md](PROGRESS.md); the why behind each component is in
[../MEMBER_A.md](../MEMBER_A.md) and [../FRAMEWORK.md](../FRAMEWORK.md).

Branches: one per milestone, stacked on the previous one and merged into the
shared `WP8-LangGraph-Recommendation-Trace` branch when tested (see
[../README.md §7](../README.md#7-branch-workflow)). M1–M2 were built on
`wp8/member-a-capture` and are merged; M3 starts on
`wp8/a-m3-invocation-helper`.
Code lives in `MetaRec-backend/langgraph_metarec/wp8_trace/` and
`MetaRec-ui/src/`. Every milestone keeps the full backend suite green.

**Dependencies on Member B** are marked **[B]**. Where B's piece is not ready,
build against the stub named in the step and swap it in later.

---

## Backend — A1 Capture & propagation

### M1 · Topology registry (D1) — target 8 Nov

1. Topology contract models: `TopologyNode`, `TopologyEdge`,
   `GraphDefinition`, `GraphInstance` (`wp8_trace/models.py`).
2. Extract definitions from the compiled graphs (`get_graph()`), with
   conditional edges labelled by their route key (`wp8_trace/topology.py`).
3. Register the five graphs with stable `@v1` IDs.
4. Record each graph's edge-router function names (the collector filters
   them out; D0 saw them as fake node runs).
5. Frozen snapshots in `tests/fixtures/wp8/topology/` plus
   `scripts/wp8_topology_snapshot.py`; tests fail on drift and point to the
   version bump.
6. `matches_registered()` so the helper (M3) can refuse a graph whose topology
   no longer matches its ID.

**Done when:** `tests/test_wp8_topology.py` passes; node IDs match
`GRAPH_EXECUTION_FLOW.md` §4.

### M2 · Event contract (D1, A leads) — target 8 Nov

1. Enums: `event_type`, `status`, `runnable_type`.
2. `GraphRunEventV1` with every top-level key present (nullable, never
   omitted) and `extra="forbid"`.
3. A validator for the required/null matrix (which fields may be null for
   each event type; root graph and `trace_truncated` exceptions).
4. Sub-shapes (`input_summary`, `state_diff`, `redaction`, `error`,
   `metadata` incl. `llm_attempt`/`tool_scope`/`tool_health`) exactly as the
   brief defines them, with the safe-value grammar and hard caps; **[B]**
   reviews them, since B owns which values may fill them.
5. Checked-in JSON Schemas in `contracts/wp8/`
   (`scripts/wp8_export_schemas.py`) with a parity test; TypeScript parity
   once the trace route is in OpenAPI (joint with **[B]**).
6. Golden trace fixtures (success, nested, repeated, failure, cancellation,
   truncation, incomplete) as `GraphTraceV1` pages — joint with **[B]**,
   needs B's page model.

**Done when:** contract tests accept every valid event and golden fixture and
reject each broken variant (missing key, wrong null, unknown field, bad enum,
over-cap value).

### M3 · Single-invocation helper (D2) — target 15 Nov

1. `TraceContext` (trace/debug-run IDs, config, sink, projector, parent
   instance and node run).
2. `invoke_with_wp8_trace(graph, input, config, trace_ctx, *, graph_definition_id)`:
   - no `trace_ctx` / disabled → exactly `graph.ainvoke(input, config)`;
   - otherwise copy-merge callbacks and WP8 metadata; never replace
     `configurable.thread_id` or other business keys; `ainvoke` once;
   - stamp the graph name / `graph_definition_id` (all graphs report
     `"LangGraph"` today).
3. Unit tests on a fake graph: config merge preserves keys, caller's dict not
   mutated, side-effect counter = 1 (off, on, error, cancel).

**Done when:** helper tests pass with a no-op collector.

### M4 · Runner plumbing + guard (D2) — target 22 Nov

1. `routing_graph`, `generic_graph`, `restaurant_graph`: add an optional
   config/`trace_ctx` parameter (default keeps today's call exactly).
2. `request_orchestrator`, `task_graph`: route their existing config through
   the helper (merge).
3. Thread `trace_ctx` from `service.py` to every nested call site
   (routing node, task domain dispatch, itinerary anchor).
4. Guard test: scan production code for `.ainvoke(` outside the helper.
5. Existing graph/service tests unchanged and green; outputs byte-identical
   with tracing off.

**Done when:** guard test passes and fails on a planted bypass.

### M5 · Callback collector (D2) — target 29 Nov

1. Handler that maps `on_chain_start/end/error` to graph/node drafts, using
   the registry for node IDs and router names.
2. Pair start/end by `run_id`; monotonic durations; error reuses the run's
   `run_id`; `CancelledError` → `cancelled`.
3. Unknown parent → `incomplete` (or drop with accounting), never reparented.
4. One `graph_instance_id` per graph run; `open_instance`/`close_instance`
   calls on the sink.
5. Every value goes through `SafeProjector` **[B]** — stub: a projector that
   returns `None` summaries and no diff.
6. Drafts go to `TraceEventSink` **[B]** — stub: in-memory list sink.
7. Re-run the D0 experiments (`tests/wp8_probe_support.py`) through the
   collector and compare parent structure with the baseline.

**Done when:** collector fixtures give the exact expected nodes and lifecycle
counts, one root, valid parents, no cycles, no router "nodes".

### M6 · Task hand-off + trace completion (D2) — target 6 Dec

1. Pass `trace_ctx` explicitly through `create_task_async` →
   `run_recommendation_task_graph` → `run_task_graph`.
2. Update `test_task_handoff_parent_depends_on_inherited_context`: a
   fresh-context hand-off must now **still link**.
3. Trace status: `running` until every open lifecycle closes; terminal job
   with open runs → `incomplete`. Root `graph_end` is not completion.
4. Nested error counts on ancestors (caught failures, E7).

**Done when:** E3/E6b/E7/E9 fixtures show the expected status and parents.

### M7 · WP5/WP7 adapter events (D2) — target 13 Dec

1. Adapter API that accepts typed WP5 attempt and WP7 scope/tool events while
   a trace is active; no-op otherwise; cannot block the caller.
2. Map to `llm_*` (`llm_attempt_adapter`), `policy_*`
   (`tool_scope_adapter`), `tool_*`; parent = current node run.

**Done when:** fake adapter events appear under the right node with the right
`runnable_type`.

### M8 · Integration + capture evidence (D2–D3) — target 24 Jan

1. Swap stubs for B's real projector and Postgres sink **[B]**.
2. Exact-once and byte-identical off/on on the full fake recommendation path.
3. 50 concurrent traced requests: no cross-trace parents, instances,
   sequences or state.

**Done when:** these three suites pass in CI; interim-report evidence ready.

---

## Frontend — A2 Live trace explorer

### M9 · Trace client + polling hook (D4) — target 20 Dec (on fixtures)

1. `getGraphTrace(runId, afterSequence, limit)` with typed errors in
   `debugApi.ts`; types from the generated OpenAPI types.
2. `useGraphTrace(runId)`: cursor polling at 1.5 s, follow `has_more`, merge
   by `sequence` with no gaps/duplicates, back off when terminal.
3. `TraceSelection` context.
4. Tests with mocked timers and paged golden fixtures.

### M10 · DAG (D4) — target 7 Feb

1. Render `topology.definitions` with nested groups per instance.
2. Six node states from events, each with text/icon as well as colour.
3. Conditional edges visually distinct; possible ≠ visited.
4. Keyboard selection and focus order.

### M11 · Timeline + selection sync (D4) — target 21 Feb

1. Events in sequence order; graph/node/type/status filters.
2. Duration bars; indentation by parent; virtualised list.
3. Selection synced with the DAG; survives new pages.
4. Integrate both views into `DebugArena.tsx` without removing existing panels.

**Done when (M9–M11):** a new event shows within two polling intervals;
render tests cover selection, keyboard, states and raw-JSON fallback.

---

## Evaluation and handover

### M12 · Evidence (D5) — target 7 Mar

1. Fidelity fixture matrix (branch, nested, task hand-off, itinerary anchor,
   repeated, synthetic parallel, adapter stubs, exception, timeout, cancel).
2. 100 paired warm runs: median ≤ 5 %, p95 ≤ 10 % overhead; no-op path
   measured separately.

### M13 · Handover (D6) — target 21 Mar

1. Rollback demo: flag off → helper is a pass-through, no rows written.
2. Runtime chapter of the report; failure analysis.
