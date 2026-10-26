# WP8 · LangGraph Recommendation Trace

WP8 adds an internal, admin-only trace of what MetaRec's LangGraph graphs
actually did during a request — which graph and node ran, in what order,
nested inside what, and where it failed — to the Debug Arena, without changing
what MetaRec does. Today the Debug Arena only shows a coarse task status.

This folder is the D0 (system understanding and baseline) work: a verified map
of how the five graphs connect, and runtime evidence of what a callback-based
collector can and cannot see. No WP8 collector exists yet.

## Contents

| Path | What it is |
|---|---|
| [GRAPH_EXECUTION_FLOW.md](GRAPH_EXECUTION_FLOW.md) | How the five graphs call each other: entry points, node topology, and the parent link at every nested call site (runtime-verified) |
| [diagrams/](diagrams/) | One diagram per graph, plus an end-to-end callback sequence (`.mmd` source + `.png`) |
| [parentage-baseline/REPORT.md](parentage-baseline/REPORT.md) | Runtime experiments on the real graphs: what was tested, results, analysis, and what it means for the collector design |
| [parentage-baseline/METHOD.md](parentage-baseline/METHOD.md) | How the evidence was produced and how to regenerate data and figures |
| [parentage-baseline/figures/](parentage-baseline/figures/) | Observed trace waterfalls |
| [parentage-baseline/data/](parentage-baseline/data/) | Raw recorded events and the summary they were analysed from |
| [team/](team/README.md) | Who builds what: the member split, the whole-framework guide, and one implementation guide per member |

Related code: [tests/test_wp8_parentage_baseline.py](../../tests/test_wp8_parentage_baseline.py)
(12 `runtime_contract` tests), [tests/wp8_probe_support.py](../../tests/wp8_probe_support.py),
[scripts/wp8_parentage_probe.py](../../scripts/wp8_parentage_probe.py),
[scripts/wp8_trace_waterfall.py](../../scripts/wp8_trace_waterfall.py).

**Reading order:** this page → `GRAPH_EXECUTION_FLOW.md` §1–§3 with the
diagrams → `parentage-baseline/REPORT.md` → `team/FRAMEWORK.md` and your own
member guide.

## Glossary

| Term | Meaning |
|---|---|
| **Node** | One named step inside a compiled graph (e.g. `intention`, `candidate_gather`). |
| **`ainvoke()`** | The async method that runs a compiled graph to completion. MetaRec has five production call sites, one per graph. |
| **Callback** | A hook LangChain/LangGraph fires when a graph, node, tool or LLM call starts or ends — what WP8 listens to instead of modifying business code. |
| **`run_id` / `parent_run_id`** | Every callback-observed unit of work gets a `run_id`; if it ran inside another one, its `parent_run_id` points there. This chain is what lets a trace view draw "this ran inside that". |
| **Graph instance** | One execution of one compiled graph. Two hotel searches in one request are two instances of the same graph. |
| **Config (`RunnableConfig`)** | The object passed through `ainvoke()` that carries callbacks, tags and checkpoint settings such as `thread_id`. If none is passed, LangChain falls back to the config of the surrounding context (a Python *contextvar*) — which works inside a node, but only implicitly. |
| **Contextvar** | A Python variable that follows the flow of execution, including into tasks started with `asyncio.create_task`. It is how parent links currently travel between graphs. |

## Key results so far

- Callbacks reconstruct the full graph and node tree across all five graphs —
  including the background-task hand-off, failure, cancellation and timeout —
  with no broken or cross-request parent links (816 events, 12 experiments).
- The background task keeps its parent but **outlives** the request that
  started it, and keeps that parent only because `asyncio.create_task` happens
  to copy the context.
- Tool and LLM calls are **invisible** to native callbacks.
- Callback data lacks graph identity (every graph is named `"LangGraph"`),
  reports cancellation as an error, and hides failures the service catches.

Details and design consequences: [parentage-baseline/REPORT.md](parentage-baseline/REPORT.md).

## Run

```bash
cd MetaRec-backend
../.venv/bin/python -m pytest -q -m runtime_contract tests/test_wp8_parentage_baseline.py
```
