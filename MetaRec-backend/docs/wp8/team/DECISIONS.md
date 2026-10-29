# WP8 Shared Decisions (Member A ↔ Member B)

Contract questions both members must agree on before D2 code depends on them,
plus the hand-offs each side is waiting for. Proposals marked **Proposed (A)**
are already implemented on the Member A branch so there is something concrete
to review; changing one means changing the model, the schema and the tests
together, which is fine — say so here first.

**How to use:** reply in the *B's answer* column (or in a PR comment), then
set the status to **Agreed** or **Changed**. Once every row is Agreed, the D1
contract is frozen.

Where things live:
- Event model and rules: [`langgraph_metarec/wp8_trace/models.py`](../../../langgraph_metarec/wp8_trace/models.py)
- Checked-in JSON Schemas: [`contracts/wp8/`](../../../../contracts/wp8/)
- Golden event lists from the D0 runs: [`tests/fixtures/wp8/golden/`](../../../tests/fixtures/wp8/golden/README.md)

---

## Hand-offs

| # | Who | What | Why | Status |
|---|---|---|---|---|
| H1 | **B → A** | **Produce the TypeScript types and send them to A.** Add the `GraphTraceV1` page model and the `GET /internal/debug/behavior-tests/{run_id}/graph-trace` route (a stub behind `DEBUG_GRAPH_TRACE_ENABLED` is enough) so `GraphRunEventV1` and the topology models appear in OpenAPI. Then run `python MetaRec-backend/scripts/export_openapi.py` and `npm run contract:gen` in `MetaRec-ui`, commit `contracts/metarec-openapi.json` and `src/contracts/openapi-types.ts`, and tell A. | The UI's types are generated from OpenAPI, and a model only enters OpenAPI once a route returns it. A's polling hook (M9) is built on these types. Needed by **20 Dec**. | Waiting on B |
| H2 | **B → A** | **Wrap the golden event lists into `GraphTraceV1` pages** once your page model exists: topology from the registry, `instances` from each manifest, `event_window`, `summary`, `redaction_summary`, `truncated`, `dropped_event_count`, `created_at`/`expires_at`. Add one page per case and at least one multi-page split (`limit` < event count) for cursor tests. | These pages are the frozen fixtures both UIs and the store test against. | Waiting on B |
| H3 | **B → A** | **Confirm the sub-shapes** of `GraphRunEventV1` (`input_summary`/`output_summary`, `state_diff`, `redaction`, `error`, `metadata` with `llm_attempt`/`tool_scope`/`tool_health`). They follow the WP8 brief exactly; you own which values may fill them. | Your redaction pipeline produces them. | Waiting on B |
| H4 | **A → B** | Golden event lists for 10 cases, generated from the real D0 recordings. | Done — see [`tests/fixtures/wp8/golden/`](../../../tests/fixtures/wp8/golden/README.md). | Done (A) |
| H5 | **A → B** | Event model, topology models, JSON Schemas with the matrix encoded, `jsonschema` dev dependency. | Done on the Member A branch. | Done (A) |

---

## Rules to agree

| # | Question | Proposal | Why | Affects | Status | B's answer |
|---|---|---|---|---|---|---|
| R1 | How is a cancelled run recorded? | One `error` event on the cancelled run's own `run_id`, with `status: "cancelled"` and `error.error_class: "CancelledError"`. No `*_end` follows. | D0: LangGraph reports cancellation and timeout as `chain_error(CancelledError)` (E9, E10). The brief says a terminal error reuses the lifecycle's `run_id`. | Store status derivation, drawer, diff severity (`cancelled` vs `error`) | Proposed (A) | |
| R2 | How is a failed run recorded? | One `error` event with `status: "error"` on the failed run's `run_id`; ancestors that catch the exception still end normally (E7). | Same mechanism as R1; matches the D0 data. | Store, drawer, nested error counts | Proposed (A) | |
| R3 | Where may `state_diff` appear? | Only on `node_end`. A failed or cancelled node has no diff. | The diff compares a node's input with its *returned* update, which exists only when the node returns. | B's differ and drawer | Proposed (A) | |
| R4 | Where may adapter metadata appear? | `llm_attempt` only on `llm_attempt_adapter` events; `tool_scope` only on `tool_scope_adapter`; `tool_health` only on `tool` or `tool_scope_adapter`. | The brief says where each "occurs"; making it a hard rule stops misplaced data. WP5/WP7 should agree too. | Adapter sink, drawer | Proposed (A) | |
| R5 | Status of start and end events | Starts are always `running` with `duration_ms: null`; ends are never `running`. | Implied by the brief, not stated. | Store, timeline | Proposed (A) | |
| R6 | Instance implies definition | Any event with a `graph_instance_id` also carries its `graph_definition_id`. | The DAG and diff need the definition for every node event. Implied, not stated. | Store, DAG, diff | Proposed (A) | |
| R7 | Who assigns `sequence` and `event_id`? | The collector emits drafts with timing (`timestamp_utc`, `monotonic_offset_ms`); the **sink** assigns `sequence` and `event_id` atomically at append. | Sequence must be allocated in one critical section with the append (brief §Atomic store); only the store can do that. | `TraceEventSink` interface | Proposed (A) | |
| R8 | Trace status for each outcome | Uncaught failure → `failed`; any cancelled run (task cancel, request timeout) → `cancelled`; crash with open runs → `incomplete`; caught nested failure → `completed` with `summary.error_count > 0`; truncation does not change status (it sets `truncated: true`). | The golden manifests record these as `expected_trace_status`. The caught-failure case (E7) is the judgement call: the run did complete, but the error must stay visible. | Store finalisation, status banner, diff | Proposed (A) | |
| R9 | What does "nesting depth 6" count? | A safe value contains at most 6 levels of lists/objects; level-7 containers are rejected. | Needed so B's redaction bounds and the model agree on the same boundary. | Redaction bounds | Proposed (A) | |
| R10 | Draft error codes | `cancelled` and `execution_failed`, with generic safe messages. | Placeholders in the golden lists; B owns the reviewed code list and messages. | Redaction, drawer | Proposed (A), B to replace | |

---

## Decided

| # | Decision | Date | Notes |
|---|---|---|---|
| X1 | Add `jsonschema==4.26.0` as a dev dependency (`requirements-dev.txt`, `pyproject.toml` dev group, `uv.lock`). Tests validate every fixture against the checked-in schemas, and the schemas now encode the required/null matrix and the safe-value caps, so a consumer using the schema alone rejects the same events Python does (except non-finite numbers, which JSON cannot carry). | 2026-10-29 | Agreed by A; B informed here |
