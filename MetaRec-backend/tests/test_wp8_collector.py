"""WP8 callback collector (M5): real runs -> GraphRunEventV1, faithful to the D0 baseline.

The golden replay runs the same D0 experiments the golden event lists were
generated from, live, through the collector, and requires the same events,
statuses and parent structure. The unit tests cover the rules the golden
cases do not reach: orphans and sealing (R11, R12), untraced graphs (R15),
pass-through runnables, native tool/LLM spans, timing, and that a failing
collector, sink or projector never touches the business run.
"""
import asyncio
import contextvars
import json
from collections import Counter
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

import wp8_probe_support as probe
from langgraph_metarec.graphs.routing_graph import build_routing_graph
from langgraph_metarec.wp8_trace import invoke_with_wp8_trace, new_trace_context, topology
from langgraph_metarec.wp8_trace.interfaces import Projection
from langgraph_metarec.wp8_trace.stubs import InMemoryTraceSink, NullProjector

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "wp8" / "golden"
ROUTERS = {"_route_after_collect_confirm", "route_after_classification", "route_after_retry"}
ROUTING_INPUT = {
    "query": "Recommend a hotel for tonight", "intent": "query", "preferences": None,
    "domain_lock": None, "force_itinerary": False, "errors": [], "retry_count": 0, "max_retries": 1,
}
OUTER_ID = "wp8-test-outer@v1"
SECRET = "sk-wp8-seeded-secret-0451"


def _trace(**options):
    sink = InMemoryTraceSink()
    ctx = new_trace_context("tr-m5", "dr-m5", sink=sink, projector=options.pop("projector", NullProjector()), **options)
    return ctx, sink


def _events(sink, trace_id="tr-m5"):
    return [json.loads(e.model_dump_json()) for e in sink.events.get(trace_id, [])]


def _shape(events):
    """Event kind, place and status, plus the (definition, node) of each event's parent run."""
    starts = {e["run_id"]: e for e in events if e["event_type"].endswith("_start")}

    def parent(e):
        p = starts.get(e["parent_run_id"]) if e["parent_run_id"] else None
        return (p["graph_definition_id"], p["node_id"]) if p else None

    return [(e["event_type"], e["graph_definition_id"], e["node_id"], e["status"], parent(e)) for e in events]


def _turn(events, turn):
    """The events of one service turn: the k-th root graph and everything under it."""
    roots = [e["run_id"] for e in events if e["event_type"] == "graph_start" and e["parent_run_id"] is None]
    keep = {roots[turn - 1]}
    for e in events:
        if e["parent_run_id"] in keep:
            keep.add(e["run_id"])
    return [e for e in events if e["run_id"] in keep]


# --------------------------------------------------------------------------
# Golden replay: the live collector reproduces every real D0 case
# --------------------------------------------------------------------------

GOLDEN_REPLAY = [
    ("full_debug_run", probe.e3_service_hotel_chain, None),
    ("query_turn_routing", probe.e3_service_hotel_chain, 1),
    ("confirm_turn_task", probe.e3_service_hotel_chain, 2),
    ("itinerary_anchor", probe.e4_itinerary_anchor, 1),
    ("repeated_domain_graphs", probe.e11_multi_domain_siblings, 2),
    ("caught_failure", probe.e7_domain_failure_caught, 2),
    ("uncaught_failure", probe.e8_routing_failure_uncaught, 1),
    ("cancellation", probe.e9_background_cancel, 2),
    ("timeout", probe.e10_request_timeout, 1),
]


@pytest.mark.backend_unit
@pytest.mark.asyncio
@pytest.mark.parametrize("case,experiment,turn", GOLDEN_REPLAY, ids=[c for c, _, _ in GOLDEN_REPLAY])
async def test_live_collector_reproduces_the_golden_case(case, experiment, turn):
    ctx, sink = _trace()

    await experiment(trace_ctx=ctx)

    events = _events(sink)
    golden = json.loads((GOLDEN_DIR / f"{case}.json").read_text())
    assert ctx.collector.internal_errors == [] and ctx.collector.dropped_event_count == 0
    assert ctx.collector.open_run_ids == []
    assert _shape(events if turn is None else _turn(events, turn)) == _shape(golden["events"])


@pytest.mark.backend_unit
@pytest.mark.asyncio
async def test_a_two_turn_trace_is_structurally_sound():
    ctx, sink = _trace()

    await probe.e3_service_hotel_chain(trace_ctx=ctx)

    events = _events(sink)
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1)), "contiguous, no lost events"
    roots = [e for e in events if e["event_type"] == "graph_start" and e["parent_run_id"] is None]
    assert [r["graph_definition_id"] for r in roots] == ["request-orchestrator@v1"] * 2, "one root per turn"
    started = set()
    for e in events:
        if e["event_type"].endswith("_start"):
            assert e["parent_run_id"] is None or e["parent_run_id"] in started, "a parent starts before its child"
            started.add(e["run_id"])
        assert e["node_id"] not in ROUTERS
    terminal = Counter(e["run_id"] for e in events if e["event_type"].endswith("_end") or e["event_type"] == "error")
    assert set(terminal) == started and set(terminal.values()) == {1}, "every run closes exactly once"
    for e in events:
        if e["event_type"].endswith("_end"):
            assert e["duration_ms"] >= 0
    offsets = [e["monotonic_offset_ms"] for e in events]
    assert offsets == sorted(offsets)


@pytest.mark.backend_unit
@pytest.mark.asyncio
async def test_graph_instances_link_to_their_invoking_node_and_close():
    ctx, sink = _trace()

    await probe.e11_multi_domain_siblings(trace_ctx=ctx)

    events = _events(sink)
    instances = sink.instances["tr-m5"]
    starts = {e["graph_instance_id"]: e for e in events if e["event_type"] == "graph_start"}
    assert set(instances) == set(starts)
    for gid, instance in instances.items():
        start = starts[gid]
        assert instance.started_sequence == start["sequence"]
        assert instance.parent_node_run_id == start["parent_run_id"]
        assert instance.parent_graph_instance_id == start["parent_graph_instance_id"]
        (end,) = [e for e in events if e["run_id"] == start["run_id"] and e["event_type"] != "graph_start"]
        assert instance.ended_sequence == end["sequence"]
    generic = [i for i in instances.values() if i.graph_definition_id == "generic-domain@v1"]
    assert len(generic) == 2 and len({i.parent_node_run_id for i in generic}) == 1, \
        "two invocations under one node run are two instances (E11)"


# --------------------------------------------------------------------------
# Rules on synthetic graphs
# --------------------------------------------------------------------------

class S(TypedDict, total=False):
    n: int
    secret: str


@pytest.fixture
def outer_graph(monkeypatch):
    """Register a one-node outer graph; returns a builder taking the node's body."""
    def build(body):
        async def call_inner(state):
            await body()
            return {"n": state.get("n", 0) + 1}

        g = StateGraph(S)
        g.add_node("call_inner", call_inner)
        g.add_edge(START, "call_inner")
        g.add_edge("call_inner", END)
        return g.compile()

    async def _noop():
        return None

    monkeypatch.setitem(topology._GRAPHS, OUTER_ID, ("Test outer graph", lambda: build(_noop)))
    topology.registered_graph.cache_clear()
    yield build
    topology.registered_graph.cache_clear()


def _outer_trace(**options):
    return _trace(turn_roots=frozenset({OUTER_ID}), **options)


async def _routing(ctx):
    await invoke_with_wp8_trace(build_routing_graph(), dict(ROUTING_INPUT), None, ctx,
                                graph_definition_id="routing-graph@v1")


@pytest.mark.backend_unit
@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_fresh_context_hand_off_is_withheld_unless_the_parent_is_explicit(outer_graph, explicit):
    """R12 on the E6b failure mode; the explicit hand-off parent (M6) links it."""
    ctx, sink = _outer_trace()

    async def spawn():
        node_run_id = var_child_runnable_config.get()["callbacks"].parent_run_id
        child_ctx = ctx.handoff(node_run_id) if explicit else ctx
        await asyncio.create_task(_routing(child_ctx), context=contextvars.Context())

    await invoke_with_wp8_trace(outer_graph(spawn), {"n": 0}, None, ctx, graph_definition_id=OUTER_ID)

    names = [e["graph_definition_id"] for e in _events(sink) if e["event_type"] == "graph_start"]
    if explicit:
        assert names == [OUTER_ID, "routing-graph@v1"] and ctx.collector.incomplete_reasons == []
    else:
        assert names == [OUTER_ID], "the parentless routing graph is not a turn root"
        assert ctx.collector.incomplete_reasons == ["orphan"]
        assert ctx.collector.dropped_event_count > 0 and len(ctx.collector.orphan_run_ids) == 1


@pytest.mark.backend_unit
@pytest.mark.asyncio
async def test_no_new_turn_root_after_the_trace_is_sealed(outer_graph):
    ctx, sink = _outer_trace()
    graph = outer_graph(lambda: asyncio.sleep(0))

    await invoke_with_wp8_trace(graph, {"n": 0}, None, ctx, graph_definition_id=OUTER_ID)
    ctx.collector.seal()
    await invoke_with_wp8_trace(graph, {"n": 0}, None, ctx, graph_definition_id=OUTER_ID)

    assert [e["event_type"] for e in _events(sink)].count("graph_start") == 1
    assert ctx.collector.incomplete_reasons == ["orphan"]


@pytest.mark.backend_unit
def test_a_run_whose_parent_never_started_is_withheld_with_its_subtree():
    ctx, sink = _trace()
    collector, stranger, child = ctx.collector, uuid4(), uuid4()

    collector.on_chain_start({}, {}, run_id=stranger, parent_run_id=uuid4(), name="routing-graph@v1")
    collector.on_chain_start({}, {}, run_id=child, parent_run_id=stranger, name="domain_classification")
    collector.on_chain_end({}, run_id=child)
    collector.on_chain_end({}, run_id=stranger)

    assert _events(sink) == []
    assert collector.dropped_event_count == 4 and collector.orphan_run_ids == [str(stranger)]


@pytest.mark.backend_unit
@pytest.mark.asyncio
async def test_a_graph_the_helper_cannot_trace_is_withheld_and_reported(outer_graph):
    """R15: a mismatched topology runs untraced under the inherited collector."""
    ctx, sink = _outer_trace()

    async def mismatched():
        await invoke_with_wp8_trace(build_routing_graph(), dict(ROUTING_INPUT), None, ctx,
                                    graph_definition_id="generic-domain@v1")

    result = await invoke_with_wp8_trace(outer_graph(mismatched), {"n": 0}, None, ctx, graph_definition_id=OUTER_ID)

    assert result == {"n": 1}
    assert {e["graph_definition_id"] for e in _events(sink)} == {OUTER_ID}
    assert ctx.collector.untraced_graphs == [("generic-domain@v1", "topology_mismatch")]
    assert len(ctx.collector.unnamed_graph_run_ids) == 1
    assert ctx.collector.incomplete_reasons == ["untraced_graph"]


@tool
def echo(text: str) -> str:
    """Echo the text."""
    return text


@tool
def broken(text: str) -> str:
    """Always fails."""
    raise ValueError("tool failed")


@pytest.mark.backend_unit
@pytest.mark.asyncio
async def test_pass_through_runnables_and_native_spans_attach_to_their_node(outer_graph):
    ctx, sink = _outer_trace()

    async def nested(_):
        await _routing(ctx)

    async def body():
        await RunnableLambda(lambda _: echo.invoke({"text": "hi"})).ainvoke(1)  # tool under a pass-through
        await FakeListChatModel(responses=["ok"]).ainvoke("a prompt")
        with pytest.raises(ValueError):
            await broken.ainvoke({"text": "x"})
        await RunnableLambda(nested).ainvoke(1)  # nested graph under a pass-through

    await invoke_with_wp8_trace(outer_graph(body), {"n": 0}, None, ctx, graph_definition_id=OUTER_ID)

    events = _events(sink)
    (node,) = [e for e in events if e["event_type"] == "node_start" and e["node_id"] == "call_inner"]
    spans = [(e["event_type"], e["runnable_type"], e["status"]) for e in events if e["runnable_type"] in ("tool", "llm")]
    assert spans == [("tool_start", "tool", "running"), ("tool_end", "tool", "completed"),
                     ("llm_start", "llm", "running"), ("llm_end", "llm", "completed"),
                     ("tool_start", "tool", "running"), ("error", "tool", "error")]
    for e in events:
        if e["runnable_type"] in ("tool", "llm"):
            assert (e["parent_run_id"], e["node_id"]) == (node["run_id"], "call_inner")
    (routing,) = [e for e in events if e["event_type"] == "graph_start" and e["graph_definition_id"] == "routing-graph@v1"]
    assert routing["parent_run_id"] == node["run_id"], "the pass-through RunnableLambda is not a parent"
    assert routing["parent_graph_instance_id"] == node["graph_instance_id"]
    assert ctx.collector.internal_errors == []


@pytest.mark.backend_unit
@pytest.mark.asyncio
async def test_durations_and_offsets_come_from_the_injected_clock(outer_graph):
    ticks = iter(range(100))
    ctx, sink = _outer_trace(clock=lambda: next(ticks) / 1000)

    await invoke_with_wp8_trace(outer_graph(lambda: asyncio.sleep(0)), {"n": 0}, None, ctx,
                                graph_definition_id=OUTER_ID)

    # One tick per clock read: graph starts at 1, node at 3, node ends at 5, graph at 7.
    events = _events(sink)
    assert [e["duration_ms"] for e in events] == [None, None, 2.0, 6.0]
    assert [e["monotonic_offset_ms"] for e in events] == [2.0, 4.0, 6.0, 8.0]


class _Exploding:
    policy_id = "metarec-debug-safe-v1"

    def __init__(self):
        self.calls = 0

    def project(self, **request):
        self.calls += 1
        raise RuntimeError("projector bug")

    def append(self, trace_id, draft):
        raise RuntimeError("sink bug")


@pytest.mark.backend_unit
@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["sink", "projector"])
async def test_a_failing_sink_or_projector_never_touches_the_run(outer_graph, broken):
    exploding = _Exploding()
    sink = exploding if broken == "sink" else InMemoryTraceSink()
    ctx = new_trace_context("tr-m5", "dr-m5", sink=sink, turn_roots=frozenset({OUTER_ID}),
                            projector=exploding if broken == "projector" else NullProjector())

    result = await invoke_with_wp8_trace(outer_graph(lambda: asyncio.sleep(0)), {"n": 0}, None, ctx,
                                         graph_definition_id=OUTER_ID)

    assert result == {"n": 1}
    assert ctx.collector.internal_errors and "collector_error" in ctx.collector.incomplete_reasons
    if broken == "projector":
        assert len(_events(sink)) == 4, "events still stored, with nothing but the fallback projection"


class _Spy:
    policy_id = "metarec-debug-safe-v1"

    def __init__(self):
        self.requests = []

    def project(self, **request):
        self.requests.append(request)
        return Projection()


@pytest.mark.backend_unit
@pytest.mark.asyncio
async def test_raw_values_reach_only_the_projector(outer_graph):
    spy = _Spy()
    ctx, sink = _outer_trace(projector=spy)

    async def body():
        await FakeListChatModel(responses=["ok"]).ainvoke(f"prompt with {SECRET}")

    await invoke_with_wp8_trace(outer_graph(body), {"n": 0, "secret": SECRET}, None, ctx,
                                graph_definition_id=OUTER_ID)

    by_event = {(r["event_type"], r["runnable_type"]): r for r in spy.requests}
    node_end = by_event[("node_end", "graph_node")]
    assert node_end["node_input"]["secret"] == SECRET and node_end["output"] == {"n": 1}, \
        "the differ gets the node's input and its returned update"
    llm_start = by_event[("llm_start", "llm")]
    assert llm_start["node_input"] is None and llm_start["output"] is None, "prompts never reach the projector"
    assert SECRET not in json.dumps(_events(sink))
