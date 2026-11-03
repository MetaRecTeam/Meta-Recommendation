# WP8 · Trace contract reference

Every shape in the graph-trace contract, what fields it carries, and which
rules it enforces. This page is a map for people; `models.py` is the law.

| Where | What it is |
|---|---|
| [`langgraph_metarec/wp8_trace/models.py`](../../langgraph_metarec/wp8_trace/models.py) | **Normative.** The models themselves. If this page and that file disagree, the file wins. |
| [`contracts/metarec-openapi.json`](../../../contracts/metarec-openapi.json) | The same shapes published to the shared interface description |
| [`MetaRec-ui/src/contracts/openapi-types.ts`](../../../MetaRec-ui/src/contracts/openapi-types.ts) | TypeScript generated from that file — never hand-written |
| [`tests/test_wp8_contract_doc.py`](../../tests/test_wp8_contract_doc.py) | Keeps this page honest: fails if a shape or a field here does not match `models.py` |

**26 shapes.** 10 are Member B's, 16 are Member A's. Every one of them
forbids unknown fields and is frozen after construction
(`ConfigDict(extra="forbid", frozen=True)`), so a typo is a validation error
rather than a silently ignored key.

**How to read a shape.** `{ a; b; c }` lists the fields. `field: Shape` means
the field holds another shape documented below, `[]` means a list of them, and
`?` marks the only fields that may be left out entirely. Everything else must
be present — a value that does not apply is `null`, never missing.

---

## 1 · What the admin asks for

The capture request, sent when an admin starts a traced debug run. Nothing is
captured unless the server flag `DEBUG_GRAPH_TRACE_ENABLED` is on; `enabled`
here cannot override it.

**`GraphTraceConfigV1`** · Member B · 7 fields

```text
{ schema_version; enabled; capture: CaptureOptions; redaction_policy_id;
  limits: TraceLimits; retention_days; correlation: TraceCorrelation? }
```

Rules: `retention_days` is 1–30 · all five `capture` keys are required · `correlation` is the only optional field.

**`CaptureOptions`** · Member B · 5 fields

```text
{ graph_lifecycle; node_lifecycle; nested_graphs; child_events; state_diff }
```

The first three are booleans. `child_events` is `none` or `tool_llm_metadata`;
`state_diff` is `none` or `allowlisted_node_update`. The brief allows no
partial capture, so a request that omits one key is rejected.

**`TraceLimits`** · Member B · 6 fields

```text
{ max_events; max_event_bytes; max_trace_bytes; max_string_chars;
  max_collection_items; max_nesting_depth }
```

A request may ask for less than the hard caps, never more. See [Hard caps](#hard-caps).

**`TraceCorrelation`** · Member B · 2 fields

```text
{ fixture_id; case_id }
```

Test labels only, matching `[A-Za-z0-9._-]{1,64}`. The pattern is deliberately
too narrow for a sentence, an email address or any other user text.

---

## 2 · What comes back

One page of a trace. The same shape serves an in-progress read and a final
one, so the UI polls the same endpoint throughout a run.

**`GraphTraceV1`** · Member B · 14 fields

```text
{ schema_version; trace_id; debug_run_id; status;
  config_snapshot: TraceConfigSnapshot; topology: TraceTopology;
  events: GraphRunEventV1[]; event_window: EventWindow;
  summary: TraceSummary; redaction_summary: RedactionSummary;
  truncated; dropped_event_count; created_at; expires_at }
```

Rules enforced by the model, which is what any store has to honour:

1. `event_window.returned` equals the number of events actually carried.
2. A page carries at most 500 events.
3. `event_window.next_after_sequence` never moves backwards from `after_sequence`.
4. `expires_at` is strictly after `created_at`.

**`TraceConfigSnapshot`** · Member B · 4 fields

```text
{ redaction_policy_id; capture: CaptureOptions; limits: TraceLimits; retention_days }
```

What the server actually applied, echoed on every page, so a reader never has
to guess which settings produced what they are looking at.

**`TraceTopology`** · Member B · 2 fields

```text
{ definitions: GraphDefinition[]; instances: GraphInstance[] }
```

Definitions are what *could* run. Instances are what *did*.

**`GraphDefinition`** · Member A · 4 fields

```text
{ graph_definition_id; name; nodes: TopologyNode[]; edges: TopologyEdge[] }
```

`graph_definition_id` is versioned, e.g. `generic-domain@v1`.

**`TopologyNode`** · Member A · 3 fields

```text
{ node_id; label; kind }
```

**`TopologyEdge`** · Member A · 4 fields

```text
{ source; target; kind; label }
```

`label` carries a stable route key on a conditional edge and is always `null`
on a normal one. Never executable source, never user data.

**`GraphInstance`** · Member A · 7 fields

```text
{ graph_instance_id; graph_definition_id; parent_graph_instance_id;
  parent_node_run_id; namespace; started_sequence; ended_sequence }
```

The two parent fields are what attach a sub-graph to the node that called it.

**`EventWindow`** · Member B · 4 fields

```text
{ after_sequence; returned; next_after_sequence; has_more }
```

Where this page sits in the trace, and what to pass to get the next one.

**`TraceSummary`** · Member B · 5 fields

```text
{ started_at; completed_at; duration_ms; visited_nodes; error_count }
```

Counts for the whole trace, not just the returned page. `completed_at` and
`duration_ms` are `null` while the run is still going.

**`RedactionSummary`** · Member B · 2 fields

```text
{ redacted_value_count; truncated_value_count }
```

So a reader knows something was withheld rather than assuming it was empty.

---

## 3 · One event inside the page

One thing that happened: a graph, node, tool, model or policy starting or
ending, plus the collector's own truncation marker.

**`GraphRunEventV1`** · Member A · 23 fields

```text
{ schema_version; event_id; trace_id; debug_run_id; sequence; timestamp_utc;
  monotonic_offset_ms; event_type; status; graph_definition_id;
  graph_instance_id; parent_graph_instance_id; node_id; run_id; parent_run_id;
  runnable_type; duration_ms; input_summary: PayloadSummary;
  output_summary: PayloadSummary; state_diff: StateDiff;
  metadata: EventMetadata; error: SafeError; redaction: RedactionInfo }
```

`sequence` gives the order; `run_id` / `parent_run_id` give the parent–child
shape. A required/null matrix in the model decides, per `event_type`, which
fields must be filled and which must be `null` — for example `graph_start` and
`graph_end` must have no `node_id`, and `trace_truncated` is a collector
marker rather than a lifecycle span, so it carries no `node_id`, `run_id` or
`parent_run_id` at all.

**`PayloadSummary`** · Member A · 5 fields

```text
{ keys; bytes; value_types; item_count; omitted_count }
```

Describes a payload without carrying it: field names and sizes, not values.

**`StateDiff`** · Member A · 5 fields

```text
{ basis; added; changed: ChangedValue; removed; omitted: Omission[] }
```

What a node changed. `changed` maps a field path to its before/after pair.
Only allowlisted fields appear; everything else is listed under `omitted` with
a reason, so nothing disappears silently. `removed` holds only explicit
deletion facts, never a field inferred to be gone because an update left it out.

**`ChangedValue`** · Member A · 2 fields

```text
{ before; after }
```

**`Omission`** · Member A · 2 fields

```text
{ path; reason }
```

`reason` is a short machine code matching `[a-z][a-z0-9_]{0,63}`.

**`EventMetadata`** · Member A · 8 fields

```text
{ domain; tool; model; attempt; task_id; llm_attempt: LlmAttempt;
  tool_scope: ToolScope; tool_health: ToolHealth }
```

Where a trace joins up with the other work packages.

**`LlmAttempt`** · Member A · 11 fields

```text
{ request_id; attempt_id; provider_id; model_id; policy_leg; outcome;
  validation_status; total_tokens; cost_usd; pricing_snapshot_id;
  provider_price_table_version }
```

One physical call to a model (WP5). On a start-only event the outcome fields
are `null`.

**`ToolScope`** · Member A · 5 fields

```text
{ catalog_id; health_snapshot_id; policy_version; eligible_tool_names;
  excluded: ExcludedTool[] }
```

The pre-LLM tool-scope decision (WP7): which tools the model was allowed to
see, and which were held back.

**`ExcludedTool`** · Member A · 2 fields

```text
{ name; reason_code }
```

**`ToolHealth`** · Member A · 5 fields

```text
{ provider_id; state; reason_code; checked_at; expires_at }
```

Provider health only. Never probe bodies, credentials or quota.

**`SafeError`** · Member A · 4 fields

```text
{ code; error_class; safe_message; retryable }
```

A classified error. No stack trace, no raw exception text.

**`RedactionInfo`** · Member A · 4 fields

```text
{ policy_id; redacted_paths; truncated_paths; stored_bytes }
```

Per event: which paths were removed or cut short, and under which policy.

---

## Fixed value lists

Anything outside these is a validation error.

| Field | Allowed values |
|---|---|
| `status` (trace) | `running` · `completed` · `failed` · `cancelled` · `incomplete` |
| `status` (event) | `running` · `completed` · `error` · `cancelled` · `incomplete` |
| `event_type` | `graph_start` · `graph_end` · `node_start` · `node_end` · `tool_start` · `tool_end` · `llm_start` · `llm_end` · `policy_start` · `policy_end` · `error` · `trace_truncated` |
| `runnable_type` | `graph` · `graph_node` · `tool` · `llm` · `policy` · `llm_attempt_adapter` · `tool_scope_adapter` · `trace_system` |
| `kind` (node) | `start` · `end` · `node` · `tool` · `llm` |
| `kind` (edge) | `normal` · `conditional` |
| `child_events` | `none` · `tool_llm_metadata` |
| `state_diff` | `none` · `allowlisted_node_update` |
| `basis` | `node_input_to_update` |

## Hard caps

Requested limits are bounded by these. A deployment may configure lower
values; nothing can raise them.

| Cap | Value | Constant |
|---|---|---|
| Events per trace | 5,000 | `MAX_EVENTS` |
| Bytes per event | 16,384 (16 KB) | `MAX_EVENT_BYTES` |
| Bytes per trace | 2,097,152 (2 MB) | `MAX_TRACE_BYTES` |
| Characters per string | 512 | `MAX_STRING_CHARS` |
| Items per collection | 50 | `MAX_COLLECTION_ITEMS` |
| Nesting depth | 6 | `MAX_NESTING_DEPTH` |
| Retention | 30 days (default 7) | `MAX_RETENTION_DAYS`, `DEFAULT_RETENTION_DAYS` |
| Events per page | 500 (default 200) | `MAX_PAGE_EVENTS`, `DEFAULT_PAGE_EVENTS` |
| Bytes per page | 524,288 (512 KB) | `MAX_PAGE_BYTES` |

## What reaches the front end today

`GET /internal/debug/behavior-tests/{run_id}/graph-trace` declares
`response_model=GraphTraceV1`, which pulls `GraphTraceV1` and everything below
it — 24 of the 26 shapes — into `contracts/metarec-openapi.json`, and from
there into the generated TypeScript. `GraphTraceConfigV1` and
`TraceCorrelation` are the two that are not there yet: nothing posts a request
body, so they have no route to reach OpenAPI through. They join when the
capture request endpoint lands (B4).

The route itself is still a stub behind `DEBUG_GRAPH_TRACE_ENABLED`: the shape
is frozen and published, the store is not built yet.

## Run the checks

```bash
cd MetaRec-backend
.venv/bin/python -m pytest -q -m backend_unit tests/test_wp8_page_contract.py tests/test_wp8_contract_doc.py
```
