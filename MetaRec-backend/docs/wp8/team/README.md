# WP8 Team: Responsibilities

How the two-person WP8 team splits the work. This replaces the earlier
responsibility matrix, which repeated its own sections, gave each member two
contradictory target lists, and left Member A with no frontend work beyond
the views and Member B with most of the visible UI plus the diff.

| File | Read it for |
|---|---|
| this page | Who owns what, why it is fair, and where the two halves meet |
| [FRAMEWORK.md](FRAMEWORK.md) | The whole WP8 system: data flow, components, contracts, interfaces, test map |
| [MEMBER_A.md](MEMBER_A.md) | Member A's features, files, interfaces, done-criteria and schedule |
| [MEMBER_B.md](MEMBER_B.md) | Member B's features, files, interfaces, done-criteria and schedule |

## 1. The rule

1. **Each member owns exactly one heavy backend feature and one heavy
   frontend feature**, and leads them end to end: design, code, tests,
   evidence and the viva answers for them.
2. **Equal effort, not equal file counts.** The estimate in §3 is checked at
   every phase review and rebalanced if one side slips.
3. **Contracts and final evidence are joint.** Both members sign off the D1
   schemas, the golden trace fixture, privacy decisions and the D6 report.
4. **Each member reviews the other's cluster** from their own side's
   perspective (§5), so nothing ships with only its author having read it.

The WP8 brief's suggested workload table puts all backend work on Member A
and all visualisation on Member B. The team has deliberately not used it,
because it would leave one member with no backend lead and the other with no
frontend lead.

## 2. The split

| | **Member A — Execution: capture → live explorer** | **Member B — Trust: storage → comparison** |
|---|---|---|
| **Heavy backend** | **A1 Capture & propagation (D2).** Topology registry, callback collector, single-invocation helper through all five runners and `service.py`, explicit trace context across the `asyncio.create_task` hand-off, WP5/WP7 adapter-event mapping, trace-status derivation | **B1 Safe trace store & API (D3) + diff engine (D5).** Redaction policy and state differ, append-only Postgres store with atomic sequence, caps and truncation, restart recovery, paged trace API, flags/auth, retention/expiry/delete, typed errors, definition-aggregate diff |
| **Heavy frontend** | **A2 Live trace explorer (D4).** Trace client and cursor-polling hook, DAG (possible vs visited, nested groups, statuses, conditional edges), timeline (sequence order, filters, duration bars, parent indentation), shared selection, keyboard access | **B2 Inspection & comparison UI (D4–D5).** Node drawer, run-diff view, trace request form, capability gating, honest states (truncated, incomplete, unavailable, disabled, expired, error), raw JSON fallback, delete action |
| **Evaluation owned** | Fidelity fixture matrix, five-runner guard, exact-once counter, byte-identical off/on output, 50-run concurrency, 100-pair overhead | Seeded privacy byte inspection (persisted/API/UI), cap/truncation fixtures, restart recovery, flags/auth/expiry/delete, frozen diff fixtures |
| **D1 contract lead** | `GraphRunEventV1`, topology definitions/instances | `GraphTraceConfigV1`, `GraphTraceV1` page, diff request/response |
| **Reviews** | B1 from the runtime side; B2's drawer against real lifecycle data | A1 from the privacy and storage side; A2 against the stored data |

Each cluster is coherent: A builds the events and then draws the path they
describe; B decides what may be kept and then builds the views that inspect
and compare what was kept.

## 3. Effort estimate

Relative points (1 ≈ a focused week of one person, including tests). These
are planning estimates, not measurements; revisit at each phase review.

| Member A | pts | Member B | pts |
|---|---:|---|---:|
| Topology registry + graph naming | 3 | Redaction pipeline, safe-value grammar, state differ | 7 |
| Callback collector (lifecycle pairing, routers, cancel mapping, status derivation) | 8 | Append-only store, atomic sequence, caps/truncation, recovery | 7 |
| Invocation helper, five runners, service plumbing, explicit task hand-off, guard test | 6 | Trace API, flags/auth, retention/expiry/delete, typed errors | 5 |
| WP5/WP7 adapter-event mapping | 2 | Diff engine + endpoint + frozen fixtures | 4 |
| **Backend subtotal** | **19** | **Backend subtotal** | **23** |
| Trace client + polling hook (cursor, dedupe, back-off) | 3 | Node drawer | 3 |
| DAG view | 7 | Run-diff view | 4 |
| Timeline + selection sync + keyboard access | 5 | Request form, capability gating, honest states, raw fallback, delete | 4 |
| **Frontend subtotal** | **15** | **Frontend subtotal** | **11** |
| Fidelity, concurrency, exact-once, overhead evaluation | 5 | Privacy, caps, auth/expiry evaluation; operator guide lead | 4 |
| **Total** | **39** | **Total** | **38** |

A's frontend is larger and B's backend is larger; the totals match, and each
member has a backend and a frontend feature of at least 11 points.

## 4. Where the halves meet

Four handshakes, all frozen before the code on either side depends on them.
Details and proposed signatures are in [FRAMEWORK.md §7](FRAMEWORK.md#7-interfaces-between-members-proposed-freeze-at-d1).

1. **D1 contract + golden trace fixture (joint).** Schemas, Python and
   TypeScript types, and one frozen `GraphTraceV1` per edge case (success,
   nested, repeated, failure, cancellation, truncation, incomplete). Both
   sides test against the same fixture before integrating.
2. **Collector → SafeProjector (A calls B).** A's collector hands raw node
   input/update, values and exceptions to B's projector and only ever emits
   what comes back. A never decides what is safe; B never sees callbacks.
3. **Collector → TraceEventSink (A produces, B stores).** A emits draft events
   in lifecycle order; B assigns the sequence, enforces caps, persists, and
   reports `accepted | truncated | dropped`. A in-memory sink from B lets A
   test before the Postgres store exists.
4. **Debug Arena state (A provides, B consumes).** A's `useGraphTrace` hook
   and `TraceSelection` context are the single source of trace data and
   selection in `DebugArena.tsx`; B's drawer and diff read them and do not
   poll separately.

## 5. Cross-review duties

| Reviewer | Reviews | Asks |
|---|---|---|
| A | B1 store/API | Does the stored order and status match what the collector emitted? Can a cancelled or still-running task be finalised too early? |
| A | B2 drawer | Do parent/children, invocation counts and durations match the real lifecycle? |
| B | A1 collector | Does any raw value reach an event without passing the projector? Is every parent observed, never inferred? |
| B | A2 explorer | Does the DAG/timeline claim anything the stored trace does not contain (visited nodes, causality from sequence)? |

## 6. Schedule

Dates follow the team plan (implementation 31 Aug 2026 – 7 Mar 2027, interim
report 25 Jan 2027, final report 22 Mar 2027). Frontend work starts against
the golden fixture before the backend is live, so the two halves run in
parallel.

| Phase | Dates | Member A | Member B | Joint gate |
|---|---|---|---|---|
| D0 | 31 Aug – 11 Oct | Done: graph call map and runtime parentage baseline | — | Baseline docs in `docs/wp8/` |
| D1 | 12 Oct – 8 Nov | Event + topology models; topology registry snapshots | Config, page and diff models; redaction policy draft; in-memory sink | Schemas + TS parity; golden fixture frozen |
| D2 | 9 – 29 Nov | Collector, helper through five runners, explicit task hand-off, adapter mapping | Redaction pipeline, state differ, projector integrated with A's collector | Exact-once counter green; E6b links |
| D3 | 30 Nov – 24 Jan | Polling hook + DAG/timeline against the golden fixture; 50-run concurrency capture test | Postgres store, sequence, caps, recovery, paged API, flags/auth/retention/delete | Live trace through the API; interim report 25 Jan |
| D4 | 25 Jan – 21 Feb | DAG + timeline live, selection sync, keyboard access | Drawer, request form, honest states, raw fallback | Views linked during polling |
| D5 | 22 Feb – 7 Mar | Fidelity matrix, 100-pair overhead, no-behaviour-change evidence | Diff engine + diff view, frozen diff fixtures, privacy byte inspection | All acceptance rows evidenced |
| D6 | 8 – 21 Mar | Backend/runtime chapter, rollback demo | Frontend/privacy chapter, operator guide | Final report 22 Mar, viva |

## 7. Joint responsibilities

- D1 schemas, golden fixtures, and Python ↔ JSON Schema ↔ OpenAPI ↔ TypeScript parity.
- The redaction allowlist itself (B implements; both approve which fields are safe).
- Integration in `DebugArena.tsx` and the end-to-end demo.
- The D6 evaluation report, failure analysis and viva.
- Keeping `docs/wp8/` accurate when the code changes.

A contribution is complete only when it has tests and evidence, and the
frontend shows only what the stored trace actually contains.
