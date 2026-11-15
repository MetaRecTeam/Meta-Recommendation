"""WP8 runner plumbing (M4): every graph call goes through the reviewed helper.

The guard is DECISIONS.md R14: an AST scan of production code, so aliases are
caught and comments are not. The end-to-end tests run a debug-run-shaped flow
through the real service with a trace context and check that every graph
reached the collector under its registered definition ID.
"""
import ast
import os
from collections import Counter
from pathlib import Path

import pytest

from langgraph_metarec.wp8_trace import TraceContext, graph_definition_ids
from wp8_probe_support import CallCounter, Gate, RecordingHandler, _hotel_chain, build_runs, e4_itinerary_anchor

BACKEND_DIR = Path(__file__).resolve().parents[1]
HELPER = Path("langgraph_metarec/wp8_trace/invocation.py")
# Graph execution entry points (Invariant 1: no streaming pass beside ainvoke).
GRAPH_METHODS = frozenset({"ainvoke", "invoke", "astream", "stream", "astream_events", "astream_log", "abatch", "batch"})
NOT_PRODUCTION = frozenset({"tests", "scripts", "alembic", "docs", "PROJECT_INSTRUCTIONS", "debug_traces", "__pycache__"})
# (relative path, attribute) -> why a non-graph object may use that name. Empty: only graphs use them today.
ALLOWED = {}


def production_files():
    for root, dirs, files in os.walk(BACKEND_DIR):
        dirs[:] = sorted(d for d in dirs if d not in NOT_PRODUCTION and not d.startswith((".", "__pytest")))
        for name in sorted(files):
            if name.endswith(".py"):
                yield (Path(root) / name).relative_to(BACKEND_DIR)


def _is_getattr_of_graph_method(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
            and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value in GRAPH_METHODS)


def bypasses(source, rel):
    """Every graph-method attribute (or getattr of one) outside the helper."""
    if Path(rel) == HELPER:
        return []
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in GRAPH_METHODS:
            attr = node.attr
        elif _is_getattr_of_graph_method(node):
            attr = node.args[1].value
        else:
            continue
        if (str(rel), attr) not in ALLOWED:
            found.append(f"{rel}:{node.lineno} .{attr}")
    return found


def helper_call_sites(source, rel):
    """(graph_definition_id or None when not a literal, location) per helper call."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name != "invoke_with_wp8_trace":
            continue
        value = next((k.value for k in node.keywords if k.arg == "graph_definition_id"), None)
        yield (value.value if isinstance(value, ast.Constant) else None), f"{rel}:{node.lineno}"


def _sources():
    return [(rel, (BACKEND_DIR / rel).read_text(encoding="utf-8")) for rel in production_files()]


@pytest.mark.backend_unit
def test_no_production_graph_call_bypasses_the_helper():
    sources = _sources()

    assert any(rel == Path("service.py") for rel, _ in sources), "the scan covers the service"
    assert [hit for rel, source in sources for hit in bypasses(source, rel)] == []


@pytest.mark.backend_unit
def test_every_registered_graph_has_exactly_one_helper_call_site():
    sites = [site for rel, source in _sources() for site in helper_call_sites(source, rel)]

    assert [where for graph_id, where in sites if graph_id is None] == [], "graph_definition_id is a literal"
    assert Counter(graph_id for graph_id, _ in sites) == Counter(graph_definition_ids())


@pytest.mark.backend_unit
@pytest.mark.parametrize("label,source", [
    ("direct call", "async def run(graph, x):\n    return await graph.ainvoke(x)\n"),
    ("streaming pass", "async def run(graph, x):\n    async for _ in graph.astream(x):\n        pass\n"),
    ("aliased call", "async def run(graph, x):\n    call = graph.ainvoke\n    return await call(x)\n"),
    ("sync invoke", "def run(graph, x):\n    return graph.invoke(x)\n"),
    ("getattr", "async def run(graph, x):\n    return await getattr(graph, 'astream_events')(x)\n"),
])
def test_guard_catches_a_planted_bypass(label, source):
    assert bypasses(source, Path("langgraph_metarec/graphs/planted.py")), label


@pytest.mark.backend_unit
def test_guard_allows_the_helper_and_reports_non_literal_ids():
    helper_call = ("async def run(graph, x, ctx):\n"
                   "    return await invoke_with_wp8_trace(graph, x, None, ctx, graph_definition_id=GRAPH_ID)\n")

    assert bypasses(helper_call, Path("langgraph_metarec/graphs/planted.py")) == []
    assert [graph_id for graph_id, _ in helper_call_sites(helper_call, "planted.py")] == [None]
    assert bypasses((BACKEND_DIR / HELPER).read_text(), HELPER) == []


# --------------------------------------------------------------------------
# A trace context reaches every runner through the helper
# --------------------------------------------------------------------------

def _ctx():
    return TraceContext(trace_id="tr-m4", debug_run_id="dr-m4", collector=RecordingHandler())


def _graphs(ctx):
    runs = build_runs(ctx.collector.events)
    graphs = [r for r in runs.values() if r["name"] in set(graph_definition_ids()) or r["name"] == "LangGraph"]
    return graphs, runs


def _parent_node(runs, graph_run):
    return runs[graph_run["parent_run_id"]]["name"] if graph_run["parent_run_id"] else None


@pytest.mark.backend_unit
@pytest.mark.asyncio
async def test_a_traced_debug_run_reaches_every_graph_of_the_hotel_chain():
    ctx = _ctx()

    outcome = await _hotel_chain("u-m4-hotel", CallCounter(), Gate(), trace_ctx=ctx)

    graphs, runs = _graphs(ctx)
    assert outcome["task_status"] == "completed"
    assert Counter(g["name"] for g in graphs) == {
        "request-orchestrator@v1": 2, "routing-graph@v1": 1, "task-graph@v1": 1, "generic-domain@v1": 1,
    }, "every graph is named by its definition, none as the default 'LangGraph'"
    assert [g["name"] for g in graphs if g["parent_run_id"] is None] == ["request-orchestrator@v1"] * 2, \
        "one root per service turn (R11)"
    assert {g["name"]: _parent_node(runs, g) for g in graphs if g["parent_run_id"]} == {
        "routing-graph@v1": "routing",
        "task-graph@v1": "domain_dispatch",
        "generic-domain@v1": "domain_graph_dispatch",
    }


@pytest.mark.backend_unit
@pytest.mark.asyncio
async def test_a_traced_itinerary_anchor_reaches_the_nested_generic_graph():
    ctx = _ctx()

    await e4_itinerary_anchor(trace_ctx=ctx)

    graphs, runs = _graphs(ctx)
    assert "LangGraph" not in {g["name"] for g in graphs}
    (generic,) = [g for g in graphs if g["name"] == "generic-domain@v1"]
    assert _parent_node(runs, generic) == "domain_dispatch"


@pytest.mark.backend_unit
@pytest.mark.asyncio
async def test_a_traced_restaurant_task_reaches_the_restaurant_graph():
    from conftest import make_service

    ctx = _ctx()
    service, _ = make_service([])
    service._get_session_context("u-m4", "c-m4")["tasks"]["task-m4"] = {
        "task_id": "task-m4", "status": "pending", "progress": 0, "message": "Task created", "result": None, "error": None,
    }

    await service.run_recommendation_task_graph(
        "task-m4", "Recommend a restaurant", {"location": "Chinatown"}, user_id="u-m4", session_id="c-m4",
        use_online_agent=False, tool_tags=["#place", "#restaurant"], trace_ctx=ctx,
    )

    graphs, runs = _graphs(ctx)
    assert service.get_task_status("task-m4", user_id="u-m4", session_id="c-m4")["status"] == "completed"
    assert [g["name"] for g in graphs] == ["task-graph@v1", "restaurant-domain@v1"]
    (restaurant,) = [g for g in graphs if g["name"] == "restaurant-domain@v1"]
    assert _parent_node(runs, restaurant) == "domain_graph_dispatch"


@pytest.mark.backend_unit
@pytest.mark.asyncio
async def test_tracing_does_not_change_the_outcome_or_repeat_work():
    off_calls, on_calls = CallCounter(), CallCounter()

    untraced = await _hotel_chain("u-m4-off", off_calls, Gate())
    traced = await _hotel_chain("u-m4-on", on_calls, Gate(), trace_ctx=_ctx())

    assert traced == untraced
    assert on_calls.count == off_calls.count > 0, "tool adapters ran exactly as often with tracing on"
