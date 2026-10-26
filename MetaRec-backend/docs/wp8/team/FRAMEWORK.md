# WP8 Framework Guide

The whole WP8 system on one page set: what gets built, how the parts connect,
the contracts between them, and who owns each part. Read this before your own
guide ([MEMBER_A.md](MEMBER_A.md) or [MEMBER_B.md](MEMBER_B.md)).

The normative specification is the WP8 proposal brief (`WP8_README.md`, kept
with the team's planning documents). Where this guide and the brief disagree,
the brief wins and this guide must be fixed. Everything marked **proposed**
is a design to freeze at the D1 contract review, not existing code.

**Status (2026-10-26):** D0 is done: the call map
([../GRAPH_EXECUTION_FLOW.md](../GRAPH_EXECUTION_FLOW.md)) and the runtime
parentage baseline ([../parentage-baseline/REPORT.md](../parentage-baseline/REPORT.md)).
No WP8 runtime code exists yet; D1–D6 are outstanding.

---

## 1. What WP8 adds

An opt-in, administrator-only trace of one real recommendation execution in the
existing Debug Arena:

- **capture**: which graphs and nodes ran, nested inside what, in what order,
  how long, and how each ended, observed from LangChain callbacks on the
  existing `ainvoke` calls, once;
- **trust**: only allowlisted, redacted, bounded data is ever stored or shown,
  in an append-only store with an atomic sequence, retention and deletion;
- **explore**: a DAG, timeline and node drawer that stay linked while the run
  is polled;
- **compare**: a definition-level diff of two traces.

When the feature flag is off, behaviour and every existing API response are
unchanged and nothing is persisted.

## 2. End-to-end data flow

```text
 Debug Arena (admin)                                   ┌─────────── B ───────────┐
  "Start behaviour test" + graph_trace_config ───────► │ POST /internal/debug/   │
                                                       │ behavior-tests          │
                                                       │ validate config, flags, │
                                                       │ admin; create trace row │
                                                       └───────────┬─────────────┘
                                                                   │ TraceContext
 ┌──────────────────────────── A ───────────────────────────────┐  ▼
 │ run_behavior_create → service → run_request_orchestrator      │
 │   invoke_with_wp8_trace(graph, input, config, trace_ctx)      │
 │     merges WP8 collector into RunnableConfig, ainvoke ONCE    │
 │   nested runners + create_task_async hand-off carry trace_ctx │
 │   explicitly (not only via the inherited contextvar)          │
 │                                                               │
 │ Collector (callbacks) ── node input / update ──► SafeProjector (B)
 │   maps on_chain_* → graph_/node_ events,          allowlist → deny rules
 │   names graphs, skips edge routers,               → patterns → bounds
 │   maps CancelledError → cancelled                  returns safe summaries
 │ Adapter sink: WP5 llm attempts, WP7 scope/tool  ◄─ and StateDiff
 │   events → llm_/policy_/tool_ events                          │
 └──────────────────────────────┬────────────────────────────────┘
                                │ draft GraphRunEventV1 (already safe)
                                ▼
 ┌──────────────────────────── B ───────────────────────────────┐
 │ TraceEventSink.append → per-trace atomic sequence + append    │
 │ event/byte caps → one trace_truncated marker + dropped count  │
 │ Postgres: traces, topology snapshot, instances, events rows   │
 │ restart recovery: unmatched starts → incomplete               │
 │ GET  …/{run_id}/graph-trace?after_sequence&limit → GraphTraceV1│
 │ POST …/graph-trace-diff → GraphTraceDiffV1 (diff engine)      │
 │ DELETE trace, expiry cleanup                                  │
 └──────────────────────────────┬────────────────────────────────┘
                                │ pages of events (1.5 s polling)
                                ▼
 ┌───────────── A ─────────────┐   ┌───────────── B ─────────────┐
 │ useGraphTrace polling hook  │──►│ Node drawer                 │
 │ DAG (topology vs visited)   │   │ Run diff view               │
 │ Timeline (sequence, parents)│◄─►│ Trace request form, states, │
 │ shared TraceSelection       │   │ raw JSON fallback, delete   │
 └─────────────────────────────┘   └─────────────────────────────┘
```

A = Member A, B = Member B. The split and its rationale are in
[README.md](README.md).

## 3. Components

| # | Component | Planned location | Owner | Deliverable |
|---|---|---|---|---|
| 1 | Contract models (Pydantic v2) + JSON Schema + TS types | `langgraph_metarec/wp8_trace/models.py`, `contracts/`, `MetaRec-ui/src/utils/types.ts` | Joint (A: event + topology models; B: config, page, diff models) | D1 |
| 2 | Topology registry | `wp8_trace/topology.py` | A | D1–D2 |
| 3 | Callback collector | `wp8_trace/callbacks.py` | A | D2 |
| 4 | Single-invocation helper + runner/service plumbing | `wp8_trace/invocation.py`, five graph runners, `service.py` | A | D2 |
| 5 | WP5/WP7 adapter sink (mapping side) | `wp8_trace/adapters.py` | A | D2 |
| 6 | Redaction policy, safe-value grammar, state differ | `wp8_trace/redaction.py` | B | D2 |
| 7 | Append-only store, sequence allocator, truncation, recovery | `wp8_trace/storage.py`, `postgres_storage.py`, Alembic migration | B | D3 |
| 8 | Trace API, flags, auth, retention, delete, typed errors | `internal/debug/router.py`, `main.py`, `wp8_trace/config.py` | B | D3 |
| 9 | Diff engine + endpoint | `wp8_trace/diff.py`, router | B | D5 |
| 10 | Trace client + polling hook | `MetaRec-ui/src/utils/debugApi.ts`, new hook | A | D4 |
| 11 | DAG + timeline + selection sync | new components under `MetaRec-ui/src/ui/`, `DebugArena.tsx` | A | D4 |
| 12 | Node drawer, run diff view, trace request form, honest states, raw JSON fallback, delete | new components, `DebugArena.tsx`, `DashboardPage.tsx` | B | D4–D5 |
| 13 | Evaluation harness and report | `tests/`, `scripts/` | Split by area (§8) | D6 |

## 4. Contracts (summary)

All objects reject unknown top-level fields, use UTC RFC 3339 timestamps and
opaque IDs, and include every top-level key (unavailable values are `null`).
Python models, JSON Schema, OpenAPI and TypeScript types must stay equivalent.

**`GraphTraceConfigV1`** (new optional `graph_trace_config` on
`POST /internal/debug/behavior-tests`): `schema_version`
`graph-trace-config/1.0`; `enabled`; `capture` with all five keys
(`graph_lifecycle`, `node_lifecycle`, `nested_graphs`, `child_events` ∈
{`none`, `tool_llm_metadata`}, `state_diff` ∈ {`none`,
`allowlisted_node_update`}); a server-registered `redaction_policy_id`;
`limits`; `retention_days` 1–30 (default 7); optional `correlation`
(`fixture_id`, `case_id`, `[A-Za-z0-9._-]{1,64}`). Supplied while the server
flag is off → typed `graph_trace_disabled` error, never silent success.

**Hard caps:** 5,000 events, 16 KiB per event, 2 MiB per trace, 512 chars per
string, 50 items per collection, nesting depth 6; one API page ≤ 500 events
and ≤ 512 KiB.

**`GraphRunEventV1`** — one row per accepted event:

- `event_type`: `graph_start|graph_end|node_start|node_end|tool_start|tool_end|llm_start|llm_end|policy_start|policy_end|error|trace_truncated`.
- `status`: `running|completed|error|cancelled|incomplete`.
- `runnable_type`: `graph|graph_node|tool|llm|policy|llm_attempt_adapter|tool_scope_adapter|trace_system`.
- `sequence` starts at 1, allocated atomically per trace; a polling cursor,
  not causality. Causality is `run_id` / `parent_run_id` plus graph instance links.
- A start/end pair shares one `run_id`; a terminal `error` reuses it. Node
  parent = the containing graph run; tool/LLM/policy parent = exactly the
  triggering node run; nested graph parent = the invoking node run. Unknown
  parents are `incomplete` or dropped with accounting, never reparented.
- `duration_ms` is `null` on starts and monotonic on matched ends;
  `timestamp_utc` is display-only.
- `input_summary`/`output_summary`: `{keys, bytes, value_types?, item_count?, omitted_count?}` or `null`.
- `state_diff`: `{basis: "node_input_to_update", added, changed, removed, omitted}`;
  `removed` only for explicit deletions; omissions are `{path, reason}`.
- `metadata` keys only `domain, tool, model, attempt, task_id, llm_attempt, tool_scope, tool_health`.
- `error`: `{code, error_class, safe_message, retryable}` or `null`.
- `redaction`: always present (`policy_id, redacted_paths, truncated_paths, stored_bytes`).

**`GraphTraceV1`** (`GET /internal/debug/behavior-tests/{run_id}/graph-trace?after_sequence=0&limit=200`):
trace `status` (`running|completed|failed|cancelled|incomplete`),
`config_snapshot`, `topology.definitions` (possible nodes/edges, edge `kind`
`normal|conditional`) and `topology.instances` (what actually ran: 7 fields
including `parent_graph_instance_id` and `parent_node_run_id`), the requested
`events` page, `event_window` (`after_sequence, returned, next_after_sequence, has_more`),
whole-trace `summary` and `redaction_summary`, `truncated`,
`dropped_event_count`, `created_at`, `expires_at`. A tracked task that was not
traced when it started returns/displays `trace_unavailable`.

**`GraphTraceDiffRequestV1` → `GraphTraceDiffV1`:** `granularity` is always
`definition_aggregate`; tolerance 0–100 %. Nodes match on
`graph_definition_id + node_id` only. Repeats: `invocation_count` counts
starts, `duration_ms` sums matched completed ends, status by severity
`error > cancelled > incomplete > completed`, changed paths are a union.
Policy-hidden values are `not_comparable`. Output: `compatibility_warnings`,
`topology_added/removed`, `visited_only_left/right`, per-node rows, summary.

## 5. Runtime facts the design must handle

From the D0 probe over the real graphs ([../parentage-baseline/REPORT.md](../parentage-baseline/REPORT.md)):

| Fact | Consequence | Owner |
|---|---|---|
| Every nested call already has a real callback node parent, via the inherited contextvar | Take parents from the callback chain; never from graph identity or domain | A |
| `task_graph` keeps its `domain_dispatch` parent across `asyncio.create_task` only because contextvars are copied (a fresh context orphans it, E6b) | Pass trace context explicitly through `create_task_async`; flip the E6b test to "still links" | A |
| The task graph outlives the request graph | Root `graph_end` ≠ trace complete; trace stays `running` until every open lifecycle closes | A (derive), B (persist) |
| All graphs report run name `"LangGraph"` | The helper stamps `graph_definition_id` per graph | A |
| Conditional-edge routers appear as child runs of nodes | Not node visits; record as edge evaluations or drop | A |
| Cancellation and timeout arrive as `chain_error(CancelledError)` | Map the exception class to `cancelled` | A |
| A failure the service catches looks like success one level up (E7) | Ancestors carry nested error counts; the UI surfaces nested errors | A (data), B (drawer) |
| Tool and LLM calls fire no native callbacks | Their events come only from the WP5/WP7 adapter sink | A |
| Inherited callback metadata carries the conversation `thread_id` | Never derive identity from inherited metadata | A |
| Two `generic_graph` instances can share one parent node run (E11) | One `graph_instance_id` per run; diff counts invocations | A (ids), B (diff) |

## 6. Non-negotiable invariants

1. **One business execution.** Never pair `astream`/`stream_events` with a
   second `ainvoke`. A side-effect counter test proves it.
2. **Explicit propagation.** One reviewed config/trace context through every
   root and nested `ainvoke`; merge, never replace, `configurable.thread_id`.
3. **No semantic change.** Prompts, tools, routing, ranking, retries,
   checkpoints, items and public response shapes are untouched.
4. **Safe before storage.** Allowlist → deny rules → patterns → bounds happen
   before disk, API, UI or the explanation endpoint. Unknown values are
   omissions, never hashes.
5. **Disabled means absent.** `DEBUG_GRAPH_TRACE_ENABLED` (new, default false)
   and `DEBUG_UI_ENABLED` and admin role are all required; with the flag off,
   responses are byte-identical and nothing is persisted.
6. **Polling is core.** Cursor polling at the existing 1.5 s cadence; SSE is
   stretch only.

## 7. Interfaces between members (proposed, freeze at D1)

These are the seams where A's and B's code meet. Names are proposals.

```python
# A provides — wp8_trace/invocation.py
async def invoke_with_wp8_trace(graph, input, config, trace_ctx, *, graph_definition_id: str): ...
    # merges collector + trace metadata into a copy of config, calls graph.ainvoke ONCE;
    # trace_ctx None or disabled -> plain graph.ainvoke(input, config)

@dataclass(frozen=True)
class TraceContext:              # A provides; created by B's route, carried by A's plumbing
    trace_id: str
    debug_run_id: str
    config: GraphTraceConfigV1
    sink: "TraceEventSink"       # B's store
    projector: "SafeProjector"   # B's redaction

# B provides — wp8_trace/redaction.py
class SafeProjector(Protocol):
    def summarize(self, value) -> Summary | None: ...
    def node_state_diff(self, graph_definition_id, node_id, node_input, node_update) -> StateDiff | None: ...
    def safe_error(self, exc: BaseException) -> SafeError: ...
    def metadata(self, raw: dict) -> dict: ...

# B provides — wp8_trace/storage.py
class TraceEventSink(Protocol):
    def append(self, trace_id: str, draft: DraftEvent) -> AppendResult: ...   # assigns sequence atomically; accepted | truncated | dropped
    def open_instance(self, trace_id: str, instance: GraphInstance) -> None: ...
    def close_instance(self, trace_id: str, graph_instance_id: str, ended_sequence: int) -> None: ...
    def finalize(self, trace_id: str, status: TraceStatus) -> None: ...
```

```ts
// A provides — MetaRec-ui
function useGraphTrace(runId: string | null): {
  trace: GraphTraceV1 | null;     // merged pages, no gaps or duplicates
  events: GraphRunEventV1[];
  status: 'idle' | 'loading' | 'polling' | 'terminal' | 'unavailable' | 'disabled' | 'error';
  error: ApiError | null;
}
type TraceSelection = { eventId?: string; graphInstanceId?: string; nodeId?: string; runId?: string };
// a React context both members' components read and write

// B provides — components consuming the above
<NodeDrawer trace={trace} selection={selection} onSelect={setSelection} />
<RunDiffView leftTraceId={...} rightTraceId={...} />
```

**Golden trace fixture (joint, D1):** one frozen `GraphTraceV1` JSON per case
(success, nested, repeated, failure, cancellation, truncation, incomplete) in
`tests/fixtures/wp8/` and mirrored for the UI tests. Both sides build and test
against it before integrating.

## 8. Testing and evaluation map

| Evidence (acceptance row) | Owner | Where |
|---|---|---|
| Five-runner guard test catches a new `ainvoke` bypass | A | `test_wp8_integration.py` |
| Fixture fidelity: visited nodes, lifecycle counts, one root, valid parents, no cycles, contiguous sequence | A | `test_wp8_collector.py`, `tests/fixtures/wp8/` |
| Side-effect counter = 1 (off / on / error / cancel); byte-identical output off vs on | A | `test_wp8_integration.py` |
| 50 concurrent traces, no cross-trace leakage | A | `test_wp8_integration.py` (uses B's store) |
| 100 paired warm runs: median ≤ 5 %, p95 ≤ 10 % overhead; no-op path separately | A | `scripts/` benchmark |
| Seeded secrets/PII absent from persisted, API and UI bytes | B | `test_wp8_redaction.py`, UI render tests |
| Over-size fixtures respect every cap with honest truncation | B | `test_wp8_storage.py` |
| Atomic append, paging without gaps/duplicates, restart → `incomplete` | B | `test_wp8_storage.py` (Postgres CI) |
| Flags, admin auth, guessed IDs, expiry, delete; flag off adds no field | B | `test_debug_task_tracking.py`, integration |
| DAG/timeline/drawer linked during polling; new event within two intervals; keyboard + non-colour cues | A (DAG/timeline), B (drawer) | `wp8.trace.render.test.tsx`, `debug.page.test.tsx` |
| Definition-aggregate diff matches frozen fixtures | B | `test_wp8_diff.py`, UI diff tests |
| Operator guide, rollback demo, failure analysis | Joint (B leads guide, A leads rollback of the helper) | `docs/` |

Existing CI (`.github/workflows/tests.yml`) runs `backend_unit` and
`runtime_contract` markers, frontend unit/render tests and the OpenAPI →
TypeScript contract check; WP8 tests should use those markers or add
`wp8_*` markers with matching CI jobs.

## 9. Further reading

- [../README.md](../README.md) — glossary (node, `ainvoke`, callback, `run_id`, graph instance, contextvar).
- [../GRAPH_EXECUTION_FLOW.md](../GRAPH_EXECUTION_FLOW.md) — the five graphs, their call sites and parentage.
- [../parentage-baseline/REPORT.md](../parentage-baseline/REPORT.md) — what callbacks see at runtime.
- [../../../internal/debug/router.py](../../../internal/debug/router.py) — current Debug Arena API and `DebugTraceStorage` (whole-file JSON, not reusable for WP8).
- [../../../../MetaRec-ui/src/ui/DebugArena.tsx](../../../../MetaRec-ui/src/ui/DebugArena.tsx) — current Debug Arena UI and its 1.5 s polling.
