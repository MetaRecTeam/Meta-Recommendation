# WP8 golden event lists

Frozen `GraphRunEventV1` streams for the D1 contract, one file per case, each
with a manifest of what the trace must show. They were generated from the real
D0 probe recordings (`docs/wp8/parentage-baseline/data/`), not written by hand:

```bash
cd MetaRec-backend
../.venv/bin/python scripts/wp8_golden_events.py
```

`tests/test_wp8_golden_events.py` fails if a file no longer matches what the
script produces, so regenerate rather than editing JSON.

| Case | Source | Shows | Expected trace status |
|---|---|---|---|
| `query_turn_routing` | E3, turn 1 | `routing_graph` nested under the orchestrator's `routing` node | completed |
| `confirm_turn_task` | E3, turn 2 | `task_graph` under `domain_dispatch` ending **after** the request graph; `generic_graph` under `domain_graph_dispatch` | completed |
| `itinerary_anchor` | E4, turn 1 | `generic_graph` under `domain_dispatch` before any task exists | completed |
| `repeated_domain_graphs` | E11, turn 2 | two `generic_graph` instances under one `domain_graph_dispatch` run | completed |
| `caught_failure` | E7, turn 2 | `generic_graph` fails; the service catches it; ancestors end normally | completed (2 errors) |
| `uncaught_failure` | E8, turn 1 | one failure closes four runs, innermost first | failed |
| `cancellation` | E9, turn 2 | the background task is cancelled mid tool call | cancelled |
| `timeout` | E10, turn 1 | the request times out while `intention` waits on the LLM | cancelled |
| `truncation` | E3 turn 2, synthetic | an 11-event cap, one `trace_truncated` marker, 17 events dropped | completed, truncated |
| `incomplete` | E3 turn 2, synthetic | process crash after `candidate_gather` starts; 6 runs left open | incomplete |

Each file is `{"manifest": …, "events": […]}`. The manifest lists the graph
instances (with parent instance and parent node run), the visited nodes per
instance, lifecycle counts, unmatched starts, error count and the expected
trace status.

**What these are not.** The D0 listener never recorded values, so summaries,
state diffs and metadata are `null` and redaction lists are empty. Member B's
redaction pipeline fills those; privacy fixtures with seeded values are
separate. Expected statuses and the error-event shape are proposals recorded
in [`docs/wp8/team/DECISIONS.md`](../../../../docs/wp8/team/DECISIONS.md)
(R1, R2, R8); the files change if those decisions change.
