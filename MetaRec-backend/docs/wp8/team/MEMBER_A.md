# Member A — Execution: Capture → Live Explorer

You own how a real MetaRec run becomes trace events, and how that execution
path is drawn. Heavy backend: **A1 Capture & propagation**. Heavy frontend:
**A2 Live trace explorer**. Read [FRAMEWORK.md](FRAMEWORK.md) first; this page
assumes its vocabulary and interfaces.

**Build plan and status:** [member-a/ROADMAP.md](member-a/ROADMAP.md) (milestones M1–M13) and
[member-a/PROGRESS.md](member-a/PROGRESS.md) (tracker).

**You provide** to Member B: draft events in lifecycle order, graph instance
open/close calls, trace-status derivation, the `useGraphTrace` hook and the
`TraceSelection` context.
**You consume** from Member B: `SafeProjector` (every value you emit passes
through it), `TraceEventSink` (in-memory first, then Postgres), and the
paged `GraphTraceV1` API.

---

## A1. Capture & propagation (backend, D1–D2)

### What to build

| Piece | File (planned) | What it does |
|---|---|---|
| Topology registry | `langgraph_metarec/wp8_trace/topology.py` | One versioned definition per compiled graph (`request-orchestrator@v1`, `routing-graph@v1`, `task-graph@v1`, `generic-domain@v1`, `restaurant-domain@v1`; see [GRAPH_EXECUTION_FLOW §4](../GRAPH_EXECUTION_FLOW.md#4-topology-ids-suggested-graph_definition_id--node_id-mapping)). Extract nodes and normal/conditional edges from the compiled graph; give conditional edges a stable safe label. Snapshot-test each definition so a node rename forces a version bump. |
| Single-invocation helper | `wp8_trace/invocation.py` | `invoke_with_wp8_trace(graph, input, config, trace_ctx, *, graph_definition_id)`: copy and merge the collector and WP8 metadata into `config` without touching `configurable.thread_id` or other business keys, then `await graph.ainvoke(...)` **once**. With no or disabled `trace_ctx` it is exactly `graph.ainvoke(input, config)`. |
| Runner plumbing | the five files in `langgraph_metarec/graphs/`, `service.py` | Route all five `ainvoke` sites through the helper. `request_orchestrator` and `task_graph` already pass config (merge). `routing_graph`, `generic_graph` and `restaurant_graph` pass none today, so add an optional `config`/`trace_ctx` parameter and thread it from the caller. |
| Explicit task hand-off | `service.py` `create_task_async` → `run_recommendation_task_graph` → `run_task_graph` | Carry `trace_ctx` and the parent node run explicitly into the background task. Today the link survives only because `asyncio.create_task` copies contextvars (E6a); with a fresh context it breaks (E6b). |
| Callback collector | `wp8_trace/callbacks.py` | A `BaseCallbackHandler` mapping `on_chain_start/end/error` (and tool/LLM callbacks if any appear) to `graph_*`, `node_*` and `error` drafts. Track open lifecycles by `run_id`, monotonic start time and observed `parent_run_id`. |
| Adapter-event mapping | `wp8_trace/adapters.py` | Accept typed WP5 LLM-attempt and WP7 tool-scope/tool-health events while a trace is active and map them to `llm_*` (`llm_attempt_adapter`), `policy_*` (`tool_scope_adapter`) and `tool_*`, parented to the current node run. A no-op when tracing is off; it can never block or alter the source operation. |
| Guard test | `tests/test_wp8_integration.py` | Scan production code for `.ainvoke(` and fail if any call bypasses the helper. |

### Rules that are easy to get wrong

These come from the D0 probe ([../parentage-baseline/REPORT.md](../parentage-baseline/REPORT.md)):

- **Graph identity:** every graph's callback run name is `"LangGraph"`. Stamp
  `graph_definition_id` in the helper (for example `with_config(run_name=…)`
  or metadata); never infer it from the first node as the probe had to.
- **Routers are not nodes:** `_route_after_collect_confirm` and
  `route_after_classification` arrive as child chain runs of nodes. Drop them
  or record them as edge evaluations; never count them as node visits.
- **Cancellation:** cancel and timeout arrive as `chain_error` with
  `CancelledError`. Map that class to status `cancelled`; other exceptions to
  `error`. Errors close the same `run_id`, innermost first.
- **Completion:** the task graph outlives the request graph. A trace is
  complete only when every open lifecycle has closed (or the job is terminal
  and the rest become `incomplete`); root `graph_end` is not the end.
- **Caught failures:** in E7 `generic_graph` fails but its ancestors and the
  task report success. Carry nested error counts upward so the UI can show
  them.
- **Identity metadata:** inherited callback metadata carries the
  *conversation* `thread_id` and the parent's `checkpoint_ns`. Never derive
  task or instance identity from it; use your own `graph_instance_id`.
- **Parents:** take `parent_run_id` from the callback; never attach a child to
  the root or to a graph because its parent is unknown — mark it `incomplete`
  or drop it with accounting.
- **Safety boundary:** pass raw node input/update and exceptions only to
  `SafeProjector`; emit only what it returns. Never put raw values in drafts.
- **Two invocations of one definition** under one node (E11) get two
  `graph_instance_id`s with the same `parent_node_run_id`.

### Done when

- Every production `ainvoke` goes through the helper; the guard test fails on
  a planted bypass.
- A side-effect counter node runs exactly once with tracing off, on, failing
  and cancelled; recommendation output and public response are byte-identical
  off vs on.
- Deterministic fixtures (simple branch, nested routing, task hand-off,
  itinerary anchor, repeated domain graphs, a synthetic parallel graph,
  adapter tool/LLM stubs, exception, timeout, cancellation) produce exactly
  the expected visited nodes and lifecycle counts, one root, valid parents,
  no cycles.
- `test_task_handoff_parent_depends_on_inherited_context` in
  `tests/test_wp8_parentage_baseline.py` is deliberately updated: with WP8
  propagation, the fresh-context hand-off **still links**.
- 50 concurrent traced requests show no cross-trace parent, instance,
  sequence or state leakage.

---

## A2. Live trace explorer (frontend, D3–D4)

### What to build

| Piece | File (planned) | What it does |
|---|---|---|
| Trace client | `MetaRec-ui/src/utils/debugApi.ts`, `types.ts` | `getGraphTrace(runId, afterSequence, limit)` with typed errors (`graph_trace_disabled`, `trace_unavailable`, 403, 404, expired). Types come from the generated OpenAPI types; do not hand-edit `src/contracts/openapi-types.ts`. |
| Polling hook | new `useGraphTrace.ts` | Poll at the existing 1.5 s cadence with `after_sequence`; keep fetching while `has_more`; merge pages by `sequence` with no gaps, duplicates or reordering; back off at terminal status; expose `status` (`loading`, `polling`, `terminal`, `unavailable`, `disabled`, `error`). This is the only poller for trace data in the page. |
| Selection model | new `TraceSelection` context | `{eventId, graphInstanceId, nodeId, runId}`; DAG, timeline and Member B's drawer all read and write it. |
| DAG | new `TraceDag.tsx` | Draw `topology.definitions` (possible nodes and edges) with nested groups per graph instance from `topology.instances`. Mark visited/running/completed/error/cancelled/incomplete from events, with text or icons as well as colour. Conditional edges look different from normal edges. Possible ≠ visited: a node is visited only if an event says so. |
| Timeline | new `TraceTimeline.tsx` | Events in `sequence` order with graph/node/type/status filters, duration bars from `duration_ms`, indentation from parent links. Label sequence as accepted order, not causality. Virtualise long lists; lazy-render nested groups. |
| Integration | `DebugArena.tsx`, `DebugPage.css` | Add the explorer to the run detail without removing the run list, coarse events, artifacts, raw JSON or explanation. |

### Done when

- Selecting a timeline event highlights its DAG node and graph instance, and
  the reverse; selection survives new pages arriving.
- A new event appears within two polling intervals in the local test setup.
- Nodes and events are keyboard-selectable with a sensible focus order;
  status is readable without colour.
- If the DAG layout fails, the timeline and raw JSON still work.
- Render tests (`wp8.trace.render.test.tsx`, `debug.page.test.tsx`) cover the
  golden fixtures, cursor merging with mocked timers, and the
  running → terminal transition.

---

## Evaluation you own (D5–D6)

| Experiment | Target |
|---|---|
| Fidelity fixture matrix (all fixtures above) | Exact expected nodes and lifecycle counts; zero lost events below limits |
| Five-runner guard + exact-once counter | 100 % of sites; counter = 1 in every mode |
| Byte-identical output | Off vs on identical for public response and persisted business result |
| 50 concurrent traces | Zero cross-trace leakage |
| 100 paired warm runs, fake adapters | Median overhead ≤ 5 %, p95 ≤ 10 %; no-op path reported separately |

Report results that miss a target as they are; do not remove slow or failing
fixtures after seeing results.

## Schedule

| Dates | Focus | Hand-off to B |
|---|---|---|
| 12 Oct – 8 Nov | Event and topology models; topology registry and snapshots; golden fixture (joint) | Frozen `GraphRunEventV1` and topology schemas |
| 9 – 29 Nov | Helper + five runners; collector; explicit task hand-off; adapter mapping; exact-once and guard tests | Collector wired to B's projector and in-memory sink |
| 30 Nov – 24 Jan | Polling hook, selection model, DAG and timeline on the golden fixture; 50-run concurrency capture | Live events into B's Postgres store; interim evidence |
| 25 Jan – 21 Feb | Explorer live against the API; keyboard access; virtualisation | Hook and selection consumed by B's drawer |
| 22 Feb – 7 Mar | Fidelity matrix, overhead benchmark, no-behaviour-change evidence | Results for the joint report |
| 8 – 21 Mar | Runtime chapter, rollback demo (flag off → helper is a pass-through) | Final report 22 Mar |

## Reviews you do for Member B

- B1 store/API: does the stored order and trace status match what the
  collector emitted, including tasks that outlive the request?
- B2 drawer: do parents, children, invocation counts and durations match the
  real lifecycle?

## Start here

1. [../GRAPH_EXECUTION_FLOW.md](../GRAPH_EXECUTION_FLOW.md) §1–§4 with the [diagrams](../diagrams/).
2. [../parentage-baseline/REPORT.md](../parentage-baseline/REPORT.md) §5 and §7.
3. Run the baseline tests and read how the probe attaches its listener:
   [../../../tests/wp8_probe_support.py](../../../tests/wp8_probe_support.py),
   [../../../tests/test_wp8_parentage_baseline.py](../../../tests/test_wp8_parentage_baseline.py).
4. Open the five runners in [../../../langgraph_metarec/graphs/](../../../langgraph_metarec/graphs/) and `service.py`'s `create_task_async`.
