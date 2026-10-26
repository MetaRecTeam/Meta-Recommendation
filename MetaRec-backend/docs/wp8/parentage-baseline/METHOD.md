# WP8 Parentage Baseline — Method and Reproduction

**For the findings and analysis, read [REPORT.md](REPORT.md).** This file
covers how the evidence was produced and how to regenerate it.

**Baseline:** branch `WP8-LangGraph-Recommendation-Trace`, commit `9c09346`,
Python 3.11.5, `langgraph==1.1.10`, `langchain-core==1.6.1`. Existing graph
test suites were green before the probe ran (111 passed, 2 skipped).

## Method

- [tests/wp8_probe_support.py](../../../tests/wp8_probe_support.py) holds the
  listener, the 12 experiments and the analysis. Both the CLI
  ([scripts/wp8_parentage_probe.py](../../../scripts/wp8_parentage_probe.py))
  and the tests
  ([tests/test_wp8_parentage_baseline.py](../../../tests/test_wp8_parentage_baseline.py))
  import it, so the published evidence and the asserted behaviour run the
  same code.
- The listener is a `BaseCallbackHandler` attached through
  `langchain_core.tracers.context.register_configure_hook`, bound to a
  contextvar that is `None` outside an experiment. **No production file,
  signature or config is modified.**
- It records only lifecycle identity: event type, `run_id`, `parent_run_id`,
  run name, monotonic offset, asyncio task name, error class, and five
  metadata keys (`langgraph_node`, `langgraph_step`, `langgraph_checkpoint_ns`,
  `checkpoint_ns`, `thread_id`). No inputs, outputs or prompts.
- Experiments reuse fixtures already in `tests/`: `conftest.make_service`
  (real `MetaRecService`, scripted fake LLM, in-memory checkpointer), the fake
  tool registries from `test_generic_graph.py` / `test_restaurant_graph.py`,
  and the hotel and itinerary-anchor service flows from `test_routing_graph.py`.
  Network-free.
- Fake tools wait on a `Gate` released only after the confirm request has
  returned, which makes "the background task outlives the request" a
  deterministic property rather than a timing accident.
- Faults are injected by temporarily patching one internal function
  (`generic_graph._rank_items` in E7, `routing_graph._explicit_query_domains`
  in E8). Cancellation uses the production path
  `MetaRecService.cancel_conversation_tasks_async` (E9). The timeout wraps a
  request whose fake LLM never answers in `asyncio.wait_for(…, 0.5)` (E10).
- Graph identity is inferred from each graph's first node, because every
  compiled graph reports the callback run name `"LangGraph"`.

## Experiments

| # | Experiment | Built from |
|---|---|---|
| E1 | `routing_graph` alone | `test_routing_graph.py` |
| E2 | `restaurant_graph` alone | `test_restaurant_graph.py` fakes |
| E3 | Hotel query → confirm → real background task | `make_service` + `test_service_hotel_query_confirms_with_stay_preferences` |
| E4 | Itinerary anchor, geocoder finds nothing | `test_service_prefers_direct_anchor_geocoder_over_broad_hotel_discovery` |
| E5 | E3 × 10 concurrently | E3 |
| E6a / E6b | `asyncio.create_task` hand-off with / without inherited context | real `run_task_graph` + one-node parent graph |
| E7 | Fault in `generic_graph`, caught by the service | E3 + injected fault |
| E8 | Fault in `routing_graph`, uncaught | E3 query turn + injected fault |
| E9 | Background task cancelled mid tool call | E3 + `cancel_conversation_tasks_async` |
| E10 | Request times out waiting on the LLM | `make_service` + hanging fake LLM |
| E11 | Multi-domain hotel + attraction | `make_service` + fake hotel/attraction tools |

**Totals:** 12 experiments, 816 events, 0 dangling parents, 0 cycles, 0
unmatched starts, 0 tool/LLM events.

## Layout

| Path | Contents |
|---|---|
| `REPORT.md` | Findings, analysis, prior-claim comparison, tests |
| `data/E*.events.json` | Raw recorded events per experiment |
| `data/E6_handoff_pair.events.json` | E6a + E6b on one time axis (for the figure) |
| `data/summary.json` | Per-experiment graph table, errors, integrity, concurrency, exactly-once |
| `figures/*.png` | Waterfall figures (indentation = observed parent; red dashed line = root graph end; red outline = run closed with an error) |

## Reproduce

All commands run from `MetaRec-backend/`. `run_id`s and millisecond timings
change between runs; parent relationships, error chains, integrity counts and
the leak count do not.

```bash
# Tests (12, runtime_contract)
../.venv/bin/python -m pytest -q -m runtime_contract tests/test_wp8_parentage_baseline.py

# Data (overwrites data/)
../.venv/bin/python scripts/wp8_parentage_probe.py --out docs/wp8/parentage-baseline/data
```

Figures — `--roots` picks which root run(s) to draw; `2` is the confirm turn.
The renderer writes SVG; the SVGs go to a temporary directory (they are
intermediate, not committed) and `rsvg-convert` turns each into the committed
PNG.

```bash
D=docs/wp8/parentage-baseline/data
F=docs/wp8/parentage-baseline/figures
T=$(mktemp -d)
W="../.venv/bin/python scripts/wp8_trace_waterfall.py"
$W $D/E3_service_hotel_chain.events.json $T/E3_service_hotel_chain.svg --title "E3 · Real service chain: hotel request, confirm, background task" --panel-titles "Turn 1 — query: routing_graph nests under the routing node|Turn 2 — confirm: task_graph keeps its callback parent but outlives the request graph"
$W $D/E4_itinerary_anchor.events.json $T/E4_itinerary_anchor.svg --title "E4 · Itinerary anchor: generic_graph nested inside domain_dispatch before any task exists" --panel-titles "Itinerary query turn"
$W $D/E6_handoff_pair.events.json $T/E6_handoff_pair.svg --title "E6 · Same asyncio.create_task hand-off, with vs without the inherited context" --panel-titles "E6a — context inherited (production behaviour): task_graph is a child of handoff_node|E6b — fresh context: the parent graph alone…|…and task_graph arrives as a disconnected root (parent_run_id = null)"
$W $D/E7_domain_failure_caught.events.json $T/E7_domain_failure_caught.svg --roots 2 --title "E7 · Failure inside generic_graph, caught by the service" --panel-titles "Confirm turn: the error closes generic_graph and candidate_gather, but task_graph and its node report success"
$W $D/E8_routing_failure_uncaught.events.json $T/E8_routing_failure_uncaught.svg --title "E8 · Uncaught failure inside routing_graph" --panel-titles "Query turn: one error closes every enclosing run, innermost first, and escapes to the caller"
$W $D/E9_background_cancel.events.json $T/E9_background_cancel.svg --roots 2 --title "E9 · Background task cancelled mid tool call (conversation delete)" --panel-titles "Confirm turn: cancellation arrives as chain_error(CancelledError) on every open run in the task"
$W $D/E10_request_timeout.events.json $T/E10_request_timeout.svg --title "E10 · Chat request times out while waiting on the LLM" --panel-titles "Query turn: the root and the open node close with CancelledError; the LLM call itself is invisible"
$W $D/E11_multi_domain_siblings.events.json $T/E11_multi_domain_siblings.svg --roots 2 --title "E11 · Multi-domain request: two generic_graph instances under one node" --panel-titles "Confirm turn: hotel and attraction graphs run one after another under the same domain_graph_dispatch run"
for svg in $T/*.svg; do rsvg-convert -b white -z 1.6 -o "$F/$(basename "${svg%.svg}").png" "$svg"; done
```
