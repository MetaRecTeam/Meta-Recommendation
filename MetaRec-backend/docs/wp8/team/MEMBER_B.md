# Member B — Trust: Storage → Comparison

You own what WP8 is allowed to keep, how it is kept, and how stored traces
are inspected and compared. Heavy backend: **B1 Safe trace store & API**
(plus the diff engine). Heavy frontend: **B2 Inspection & comparison UI**.
Read [FRAMEWORK.md](FRAMEWORK.md) first; this page assumes its vocabulary and
interfaces.

**You provide** to Member A: `SafeProjector` (A's collector emits only what it
returns), `TraceEventSink` (an in-memory version early, then Postgres), the
trace-creation route that builds `TraceContext`, and the paged
`GraphTraceV1` API.
**You consume** from Member A: draft events, graph-instance open/close calls,
trace-status derivation, the `useGraphTrace` hook and the `TraceSelection`
context.

---

## B1. Safe trace store & API (backend, D1–D3, diff in D5)

### What to build

| Piece | File (planned) | What it does |
|---|---|---|
| Config + flags | `langgraph_metarec/wp8_trace/config.py`, `main.py`, `ENVIRONMENT_VARIABLES.md` | `GraphTraceConfigV1` validation: hard caps, `retention_days` 1–30, registered `redaction_policy_id` only, safe `correlation` IDs. New `DEBUG_GRAPH_TRACE_ENABLED` (default false); tracing needs it **and** `DEBUG_UI_ENABLED` **and** the admin role. |
| Redaction + state differ | `wp8_trace/redaction.py` | The `SafeProjector`. Pipeline: allowlist → denied-key rules → value-pattern rules → structural limits. Bounded safe-value grammar (null/bool/finite number, ≤ 512-char string, ≤ 50-item list/object, depth ≤ 6). Unknown fields become `{path, reason: "not_allowlisted"}` — never a hash. Summaries `{keys, bytes, …}`; `node_state_diff` compares the allowlisted node input with its allowlisted update; `removed` only for explicit deletions; `safe_error` returns `{code, error_class, safe_message, retryable}` without raw exception text. |
| Append-only store | `wp8_trace/storage.py`, `postgres_storage.py`, a new Alembic migration | Tables for trace metadata + config/topology snapshot, graph instances, and one row per event keyed `(trace_id, sequence)`. Sequence allocation and append in one transaction with a uniqueness guarantee — no read-modify-write. The current `DebugTraceStorage` (one JSON file per run, whole-file rewrite, lock only around `save()`) is not reusable. Also ship an in-memory implementation of the same interface for unit tests and for A's early integration. |
| Caps and truncation | same | Enforce 5,000 events, 16 KiB/event, 2 MiB/trace (topology bytes count too). When the limit is reached, reserve room for exactly one `trace_truncated` event, record `dropped_event_count`, and stop accepting ordinary events. |
| Recovery | same | On start-up, traces with open lifecycles and no live job become `incomplete`; unmatched starts stay visible as such. Persist a terminal marker where possible. |
| Trace API | `internal/debug/router.py` | Optional `graph_trace_config` on `POST /behavior-tests` (existing fields and start response unchanged; flag off → typed `graph_trace_disabled`). `GET /behavior-tests/{run_id}/graph-trace?after_sequence&limit` (limit 1–500, ≤ 512 KiB page, whole-trace summary counts). Tracking a task that was not traced when it started → `trace_unavailable`. Admin delete; expiry cleanup at start-up and on a schedule. Every route behind the existing `require_auth` (flag + admin); guessed IDs never bypass it. |
| Diff engine | `wp8_trace/diff.py`, a diff route | `GraphTraceDiffRequestV1 → GraphTraceDiffV1`, `definition_aggregate` only. Match on `graph_definition_id + node_id`; count starts; sum matched completed durations; status by `error > cancelled > incomplete > completed`; union of changed state paths; `duration_delta_percent` null when the left duration is null or 0; policy-hidden values `not_comparable`; warnings for schema, policy, incomplete or truncated mismatches. Both traces must exist, be unexpired and be accessible. |

### Rules that are easy to get wrong

- **Bytes, not return values.** Privacy tests inspect what is in Postgres and
  in API/UI payloads, not what the sanitiser function returned.
- **Sequence is a cursor.** Atomic and contiguous below limits, but it does
  not mean causality; never sort or reparent by it.
- **The explanation endpoint** may only ever see the already-sanitised trace.
- **Retention on the deployed Space.** The Hugging Face Space loses its
  filesystem on sleep/rebuild, which is one reason to store traces in
  Postgres. If any part stays local, say so; do not claim 30-day retention
  that cannot be delivered.
- **Flag off means absent:** no new fields in existing responses, no rows
  written.
- **Status from A, durability from you.** A's collector decides when a trace
  is complete (the task graph outlives the request graph); your store must
  not mark a trace `completed` just because the root graph ended.

### Done when

- Every seeded pattern (authorization/bearer values, API keys, cookies,
  passwords/secrets, emails, phone numbers, stable user IDs) in keys, values,
  nested collections, exceptions and over-size fields is absent from
  persisted bytes and API/UI payloads.
- Every over-size fixture respects each cap and carries honest truncation
  metadata.
- Concurrent appends never produce duplicate or missing sequences; paging
  with any `limit` returns every event once, in order.
- A simulated crash after starts and before ends yields an `incomplete`
  trace with the unmatched starts visible.
- Flags off, non-admin, guessed ID, expired and deleted traces all behave as
  specified, with integration tests; flag off adds no field to existing
  outputs.
- `test_wp8_diff.py` matches frozen structural, status, invocation-count,
  summed-duration and state-path-union fixtures exactly.

---

## B2. Inspection & comparison UI (frontend, D4–D5)

### What to build

| Piece | File (planned) | What it does |
|---|---|---|
| Trace request form | `DebugArena.tsx` | Optional "capture graph trace" on the behaviour-test form building a valid `GraphTraceConfigV1` (policy from the server list, limits within caps, retention). Hidden when the capability is off. |
| Capability gating | `DashboardPage.tsx`, `debugApi.ts` | Show tracing only when the server reports both flags and the user is admin. |
| Honest states | new `TraceStateBanner.tsx` | `running`, `completed`, `failed`, `cancelled`, `incomplete`, truncated (with dropped count), `trace_unavailable`, disabled, expired/deleted, API error — each with explicit text. Never invent a path from coarse task events. |
| Node drawer | new `NodeDrawer.tsx` | For the selected node/event: safe input/output summaries, allowlisted state diff, duration, invocation count, error, parent and children, nested error count, and explicit redacted/truncated/omitted markers. An omitted value is labelled omitted, never shown as empty. |
| Run diff view | new `RunDiffView.tsx` | Pick two accessible traces; show topology added/removed, visited-only-left/right, per-node status, invocation count, summed duration with tolerance, changed state paths and compatibility warnings. Label it as a definition-level aggregate. |
| Raw fallback and delete | `DebugArena.tsx` | Raw JSON of the trace page stays available if any view fails; admin delete with confirmation. |

Read trace data and selection only from A's `useGraphTrace` hook and
`TraceSelection` context; do not poll separately.

### Done when

- Drawer content for every golden-fixture node matches the stored data,
  including omission and truncation markers.
- Each honest state renders from its fixture with readable text, not colour
  alone.
- The diff view renders the frozen diff fixtures exactly and shows the
  aggregation label and every warning.
- No seeded secret appears in rendered UI output (checked in render tests).
- Render tests (`wp8.trace.render.test.tsx`, `debug.page.test.tsx`,
  `dashboard.page.test.tsx`) and client tests (`api.unit.test.ts`) pass.

---

## Evaluation you own (D5–D6)

| Experiment | Target |
|---|---|
| Seeded privacy fixtures, persisted/API/UI bytes | Zero occurrences of any seeded pattern |
| Over-size fixtures | Every cap respected; truncation metadata honest; UI stays responsive |
| Restart / incomplete recovery | Unmatched starts shown as `incomplete` |
| Flags, admin auth, expiry, delete, guessed IDs | All integration tests pass |
| Frozen diff fixtures | Exact match |

You lead the operator guide (enablement, retention limits, delete, rollback
with Member A's input on the helper).

## Schedule

| Dates | Focus | Hand-off to A |
|---|---|---|
| 12 Oct – 8 Nov | Config, page and diff models; redaction policy draft; in-memory sink; golden fixture (joint) | Frozen `GraphTraceConfigV1`/`GraphTraceV1` schemas; `TraceEventSink` interface |
| 9 – 29 Nov | Redaction pipeline, safe-value grammar, state differ; privacy unit tests | `SafeProjector` wired into A's collector |
| 30 Nov – 24 Jan | Postgres store and migration, sequence, caps/truncation, recovery, paged API, flags/auth, retention/delete | Live paged API for A's hook; interim evidence |
| 25 Jan – 21 Feb | Drawer, request form, capability gating, honest states, raw fallback | Drawer reading A's selection |
| 22 Feb – 7 Mar | Diff engine and endpoint, diff view, frozen diff fixtures; privacy byte inspection | Results for the joint report |
| 8 – 21 Mar | Privacy/frontend chapter, operator guide | Final report 22 Mar |

## Reviews you do for Member A

- A1 collector: can any raw value reach an event without passing your
  projector? Is every parent observed rather than inferred?
- A2 explorer: does the DAG or timeline claim anything the stored trace does
  not contain, such as unvisited nodes shown as visited, or causality read
  from sequence?

## Start here

1. [FRAMEWORK.md](FRAMEWORK.md) §4 (contracts) and §6 (invariants).
2. [../../../internal/debug/router.py](../../../internal/debug/router.py): `DebugTraceStorage`, `require_auth`, `BehaviorTestCreateRequest`, `run_behavior_create`.
3. The existing Alembic setup in [../../../alembic/](../../../alembic/) and the Postgres CI job in `.github/workflows/tests.yml`.
4. [../../../../MetaRec-ui/src/ui/DebugArena.tsx](../../../../MetaRec-ui/src/ui/DebugArena.tsx) and [../../../../MetaRec-ui/src/utils/debugApi.ts](../../../../MetaRec-ui/src/utils/debugApi.ts).
5. [../parentage-baseline/REPORT.md](../parentage-baseline/REPORT.md) §5.3–§5.5 (failure, cancellation, repeated graphs — what your store and diff must represent).
