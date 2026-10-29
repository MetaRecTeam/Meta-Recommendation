# Member A Progress Tracker

Status of each milestone in [ROADMAP.md](ROADMAP.md). Update the table and
add a log entry (newest first) with every change on a Member A branch. Record only what was run and verified.

**Legend:** ✅ done · 🟡 in progress · ⬜ not started · ⛔ blocked

## Milestones

| # | Milestone | Deliverable | Target | Status | Branch | Evidence |
|---|---|---|---|---|---|---|
| M1 | Topology registry | D1 | 8 Nov | ✅ | merged to shared | `tests/test_wp8_topology.py` (25 tests) |
| M2 | Event contract (`GraphRunEventV1`) | D1 | 8 Nov | 🟡 | merged to shared | `tests/test_wp8_event_contract.py` (108 tests), `tests/test_wp8_golden_events.py` (36 tests); waiting on B for H1–H3 and R1–R10 in `DECISIONS.md` |
| M3 | Single-invocation helper | D2 | 15 Nov | ⬜ | `wp8/a-m3-invocation-helper` | — |
| M4 | Runner plumbing + guard test | D2 | 22 Nov | ⬜ | — | — |
| M5 | Callback collector | D2 | 29 Nov | ⬜ | — | — |
| M6 | Task hand-off + trace completion | D2 | 6 Dec | ⬜ | — | — |
| M7 | WP5/WP7 adapter events | D2 | 13 Dec | ⬜ | — | — |
| M8 | Integration + capture evidence | D2–D3 | 24 Jan | ⬜ | — | needs B's projector and store |
| M9 | Trace client + polling hook | D4 | 20 Dec | ⬜ | — | — |
| M10 | DAG | D4 | 7 Feb | ⬜ | — | — |
| M11 | Timeline + selection sync | D4 | 21 Feb | ⬜ | — | — |
| M12 | Fidelity + overhead evidence | D5 | 7 Mar | ⬜ | — | — |
| M13 | Handover | D6 | 21 Mar | ⬜ | — | — |

## Open items and decisions

Shared items now live in [../DECISIONS.md](../DECISIONS.md) (hand-offs
H1–H5, rules R1–R10, decided X1). Member A's side of H4 and H5 is done.

## Log

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
