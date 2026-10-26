"""WP8 baseline probe support: passive callback recording over the real graphs.

Shared by tests/test_wp8_parentage_baseline.py and scripts/wp8_parentage_probe.py
so the asserted behaviour and the published evidence come from the same code.

A RecordingHandler is attached through langchain_core's configure hook bound to
a contextvar, so no production function, signature or config is modified. It
records lifecycle identity only — never inputs, outputs or prompts. Every
experiment reuses fixtures that already exist in tests/ and is network-free.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
for _p in (str(BACKEND_DIR), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from langchain_core.tracers.context import register_configure_hook  # noqa: E402

_METADATA_KEYS = ("langgraph_node", "langgraph_step", "langgraph_checkpoint_ns", "checkpoint_ns", "thread_id")


class RecordingHandler(BaseCallbackHandler):
    run_inline = True

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        self._t0 = time.perf_counter()

    def _record(self, event, run_id, parent_run_id, name=None, metadata=None, tags=None, extra=None):
        try:
            task = asyncio.current_task()
            task_name = task.get_name() if task else None
        except RuntimeError:
            task_name = None
        md = metadata or {}
        self.events.append({
            "seq": len(self.events) + 1,
            "event": event,
            "name": name,
            "run_id": str(run_id),
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "t_ms": round((time.perf_counter() - self._t0) * 1000, 3),
            "asyncio_task": task_name,
            "metadata": {k: md.get(k) for k in _METADATA_KEYS if md.get(k) is not None},
            "tags": list(tags or []),
            **(extra or {}),
        })

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        self._record("chain_start", run_id, parent_run_id, kwargs.get("name") or (serialized or {}).get("name"), metadata, tags)

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
        self._record("chain_end", run_id, parent_run_id)

    def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._record("chain_error", run_id, parent_run_id, extra={"error_class": type(error).__name__})

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        self._record("tool_start", run_id, parent_run_id, kwargs.get("name"))

    def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs):
        self._record("tool_end", run_id, parent_run_id)

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs):
        self._record("llm_start", run_id, parent_run_id, kwargs.get("name"))

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        self._record("llm_start", run_id, parent_run_id, kwargs.get("name"))

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
        self._record("llm_end", run_id, parent_run_id)


PROBE_HANDLER: contextvars.ContextVar[Optional[RecordingHandler]] = contextvars.ContextVar(
    "wp8_probe_handler", default=None
)
register_configure_hook(PROBE_HANDLER, inheritable=True)


async def record(experiment: Callable[[], Awaitable[Dict[str, Any]]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run one experiment with a fresh recorder attached; return (events, outcome)."""
    handler = RecordingHandler()
    token = PROBE_HANDLER.set(handler)
    try:
        outcome = await experiment()
    finally:
        PROBE_HANDLER.reset(token)
    return handler.events, outcome


# --------------------------------------------------------------------------
# Fixtures (same shapes as tests/test_generic_graph.py, test_restaurant_graph.py,
# test_routing_graph.py), with optional gates so ordering is deterministic.
# --------------------------------------------------------------------------

class Gate:
    """Blocks fake tool adapters (which run in worker threads via asyncio.to_thread)
    until released, and signals when the first adapter has been entered."""

    def __init__(self, released: bool = False) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        if released:
            self.release.set()

    def wait_in_adapter(self) -> None:
        self.entered.set()
        self.release.wait(10)


class CallCounter:
    def __init__(self) -> None:
        self.count = 0


def fake_place_registry(counter: CallCounter, gate: Optional[Gate] = None):
    """Hotel and attraction tools from tests/test_generic_graph.py end-to-end fixtures."""
    from langgraph_metarec.tool_registry import ToolRegistry, ToolSpec

    gate = gate or Gate(released=True)

    def make(rows):
        def adapter(params):
            counter.count += 1
            gate.wait_in_adapter()
            return [dict(row, searched_location=params.get("location")) for row in rows]
        return adapter

    specs = [
        ("gmap.hotel.search", "hotel", "#hotel", [{"title": "Palm View Hotel", "address": "7 Palm Ave", "rating": 4.6, "reviews": 512, "type": "Hotel"}]),
        ("osm.hotel.discover", "hotel", "#hotel", [{"title": "Palm Hostel", "tourism": "hostel", "address": "9 Palm Ave"}]),
        ("gmap.attraction.search", "attraction", "#attraction", [{"title": "Skyline Luge", "address": "1 Imbiah Rd", "rating": 4.7, "reviews": 8200, "type": "Attraction"}]),
        ("osm.attraction.discover", "attraction", "#attraction", [{"title": "Fort Siloso", "tourism": "attraction", "address": "Siloso Rd"}]),
    ]
    registry = ToolRegistry()
    for name, domain, tag, rows in specs:
        registry.register(ToolSpec(name=name, domain=domain, tags={"#place", tag},
                                   input_schema={"type": "object"}, output_schema={"type": "array"},
                                   adapter=make(rows)))
    return registry


def intent_payload(query: str, preferences: Dict[str, Any]) -> str:
    return json.dumps({"intent": "query", "reply": "Sure.", "confidence": 0.9, "preferences": preferences})


HOTEL_QUERY = "Find a 4-star hotel near Sentosa"
HOTEL_PREFS = {"domain": "hotel", "query": HOTEL_QUERY, "location": "Sentosa", "stars": "4"}
MULTI_QUERY = "Recommend a hotel and attractions in Sentosa"


class patched:
    def __init__(self, target, name, value):
        self.target, self.name, self.value = target, name, value

    def __enter__(self):
        self.original = getattr(self.target, self.name)
        setattr(self.target, self.name, self.value)

    def __exit__(self, *exc):
        setattr(self.target, self.name, self.original)


@contextmanager
def fake_tools(counter: CallCounter, gate: Optional[Gate] = None):
    import langgraph_metarec.graphs.generic_graph as generic_graph_module

    with patched(generic_graph_module, "DEFAULT_TOOL_REGISTRY", fake_place_registry(counter, gate)):
        yield


async def _confirm_chain(user: str, query: str, preferences: Dict[str, Any], scripted: List[Any],
                         counter: CallCounter, gate: Gate, after_task_created=None) -> Dict[str, Any]:
    """Real MetaRecService: query turn -> confirm turn -> real create_task_async -> background task.

    The fake tools wait on ``gate`` until the confirm turn has returned, so the
    background task always outlives the request in the same, repeatable order."""
    from conftest import make_service

    service, _ = make_service(scripted)
    conv = f"c-{user}"
    with fake_tools(counter, gate):
        first = await service.handle_user_request_async(query, user_id=user, session_id=conv, conversation_history=[])
        second = await service.handle_user_request_async(
            "Yes, that's correct", user_id=user, session_id=conv, conversation_history=[],
            hitl_state={**first["hitl_state"], "action": "confirm"})
        task_id = second.get("task_id")
        background = service._running_tasks.get(task_id)
        outcome: Dict[str, Any] = {"first_type": first["type"], "second_type": second["type"]}
        if after_task_created is not None:
            outcome.update(await after_task_created(service, user, conv, background, gate))
        else:
            gate.release.set()
            if background is not None:
                await background
        status = service.get_task_status(task_id, user_id=user, session_id=conv) or {}
    result = status.get("result")
    items = getattr(result, "items", []) if result is not None else []
    outcome.update({"task_status": status.get("status"),
                    "item_titles": [getattr(i, "title", None) for i in items],
                    "tool_adapter_calls": counter.count})
    return outcome


def _hotel_chain(user: str, counter: Optional[CallCounter] = None, gate: Optional[Gate] = None, after=None):
    return _confirm_chain(user, HOTEL_QUERY, HOTEL_PREFS,
                          [intent_payload(HOTEL_QUERY, HOTEL_PREFS), "Looking for a 4-star hotel near Sentosa — is that correct?"],
                          counter or CallCounter(), gate or Gate(), after)


# --------------------------------------------------------------------------
# Experiments
# --------------------------------------------------------------------------

async def e1_routing_only() -> Dict[str, Any]:
    from langgraph_metarec.graphs.routing_graph import run_routing_graph

    route = await run_routing_graph(query="Recommend a hotel for tonight", intent="query")
    return {"route_domain": route.domain, "route_mode": route.mode}


async def e2_restaurant_only() -> Dict[str, Any]:
    from langgraph_metarec.graphs.restaurant_graph import RestaurantGraphAdapters, run_restaurant_graph
    from langgraph_metarec.tool_registry import ToolRegistry, ToolSpec

    registry = ToolRegistry()
    registry.register(ToolSpec(name="gmap.search", domain="restaurant", tags={"#place", "#restaurant", "#map", "#review"},
                               input_schema={"type": "object"}, output_schema={"type": "array"},
                               adapter=lambda params: [{"title": "Graph Bistro", "rating": 4.7, "reviews": 100}]))

    def summarizer(client, user_input, gmap, xhs, yelp, model):
        return json.dumps({"recommendations": [{"name": "Graph Bistro", "area": "Chinatown", "rating": 4.7,
                                                "reviews_count": 100, "why": "probe"}]})

    result = await run_restaurant_graph(
        client=object(), summary_model="s", planning_model="p", query="Recommend ramen in Chinatown",
        preferences={"location": "Chinatown"}, user_input="{}", use_online_agent=True,
        tool_tags=["#place", "#restaurant"],
        adapters=RestaurantGraphAdapters(
            tool_registry=registry,
            planner=lambda client, user_input, model: [{"name": "gmap.search", "parameters": {"query": "ramen"}}],
            plan_parser=lambda response: response, summarizer=summarizer))
    return {"checked": [r["name"] for r in result.checked_restaurants]}


async def e3_service_hotel_chain() -> Dict[str, Any]:
    return await _hotel_chain("u-probe-hotel")


async def e4_itinerary_anchor() -> Dict[str, Any]:
    """tests/test_routing_graph.py anchor fixture with a geocoder that finds nothing,
    so domain_dispatch falls through to the real nested generic_graph call."""
    from conftest import make_service, query_intent_json
    import langgraph_metarec.tool_registry as tool_registry_module

    constraints = json.dumps({
        "location": "Sentosa", "date": "2026-08-03", "start_time": "09:00", "end_time": "19:00",
        "timezone": "Asia/Singapore", "budget_mode": "unlimited", "style": "sightseeing", "pace": "balanced",
        "hotel_anchor": "Soliso Beach Resort", "anchor_policy": "round_trip",
    })
    service, _ = make_service([query_intent_json(), constraints])
    counter = CallCounter()
    with patched(tool_registry_module, "geocode_anchor_candidates", lambda q, d, max_results=4: []), fake_tools(counter):
        result = await service.handle_user_request_async(
            "Plan a day in Sentosa starting from my hotel", user_id="u-probe-anchor",
            session_id="c-probe-anchor", conversation_history=[], itinerary_mode=True)
    return {"type": result["type"], "tool_adapter_calls": counter.count}


async def e5_concurrent_chains(n: int = 10) -> Dict[str, Any]:
    results = await asyncio.gather(*[_hotel_chain(f"u-conc-{i}") for i in range(n)])
    return {"n": n, "statuses": [r["task_status"] for r in results]}


async def _handoff(fresh_context: bool) -> Dict[str, Any]:
    """A one-node parent graph spawns the real run_task_graph through asyncio.create_task,
    as service.create_task_async does. fresh_context gives the task a new Context holding
    only the probe handler — a hand-off that does not carry var_child_runnable_config."""
    from typing import TypedDict

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph_metarec.graphs.task_graph import TaskGraphAdapters, run_task_graph
    from langgraph_metarec.state import DomainGraphResult

    parent_done = asyncio.Event()

    async def fake_domain(progress_callback):
        await parent_done.wait()
        return {"domain_graph_result": DomainGraphResult(domain="hotel", status="completed"),
                "result_object": {}, "result_payload": {}, "metadata": {}}

    async def write_projection(payload):
        return None

    spawned: List[asyncio.Task] = []
    label = "fresh" if fresh_context else "inherited"

    class S(TypedDict):
        mode: str

    async def handoff_node(state: S) -> S:
        coro = run_task_graph(adapters=TaskGraphAdapters(run_domain_graph=fake_domain, write_projection=write_projection),
                              user_id="u-handoff", conversation_id=f"c-{label}", branch_id=None,
                              task_id=f"t-{label}", query="probe", checkpointer=MemorySaver())
        if fresh_context:
            ctx = contextvars.Context()
            ctx.run(PROBE_HANDLER.set, PROBE_HANDLER.get())
            spawned.append(asyncio.create_task(coro, context=ctx))
        else:
            spawned.append(asyncio.create_task(coro))
        return state

    g = StateGraph(S)
    g.add_node("handoff_node", handoff_node)
    g.add_edge(START, "handoff_node")
    g.add_edge("handoff_node", END)
    await g.compile().ainvoke({"mode": label})
    parent_done.set()
    await asyncio.gather(*spawned)
    return {"handoff": label}


async def e6a_handoff_inherited_context() -> Dict[str, Any]:
    return await _handoff(fresh_context=False)


async def e6b_handoff_fresh_context() -> Dict[str, Any]:
    return await _handoff(fresh_context=True)


async def e7_domain_failure_caught() -> Dict[str, Any]:
    """An exception inside generic_graph (``_rank_items`` is first called while
    candidate_gather collects items). The service wraps the domain graph in
    try/except and returns an explained empty result."""
    import langgraph_metarec.graphs.generic_graph as generic_graph_module

    def boom(items):
        raise RuntimeError("probe-injected ranking failure")

    with patched(generic_graph_module, "_rank_items", boom):
        return await _hotel_chain("u-probe-fail")


async def e8_routing_failure_uncaught() -> Dict[str, Any]:
    """An exception inside routing_graph's domain_classification node on the query turn.
    ``_explicit_query_domains`` runs for every non-itinerary query (``classify_domain``
    is skipped when the LLM preferences already name a domain)."""
    from conftest import make_service
    import langgraph_metarec.graphs.routing_graph as routing_graph_module

    def boom(query):
        raise RuntimeError("probe-injected classification failure")

    service, _ = make_service([intent_payload(HOTEL_QUERY, HOTEL_PREFS)])
    with patched(routing_graph_module, "_explicit_query_domains", boom):
        try:
            result = await service.handle_user_request_async(
                HOTEL_QUERY, user_id="u-probe-route-fail", session_id="c-probe-route-fail", conversation_history=[])
            return {"raised": None, "type": result.get("type")}
        except Exception as exc:  # noqa: BLE001 — the probe records whatever escapes
            return {"raised": type(exc).__name__}


async def e9_background_cancel() -> Dict[str, Any]:
    """Cancel a running background task through the real conversation-delete path
    (service.cancel_conversation_tasks_async) while generic_graph is mid-tool-call."""

    async def cancel_mid_flight(service, user, conv, background, gate):
        await asyncio.to_thread(gate.entered.wait, 10)
        summary = await service.cancel_conversation_tasks_async(user, conv)
        try:
            await background
            awaited = "completed"
        except asyncio.CancelledError:
            awaited = "CancelledError"
        gate.release.set()
        return {"cancel_summary_cancelled": summary.get("cancelled"), "background_await": awaited}

    return await _hotel_chain("u-probe-cancel", after=cancel_mid_flight)


async def e10_request_timeout() -> Dict[str, Any]:
    """The chat request itself times out while the intention node awaits the LLM
    (e.g. a server timeout or client disconnect cancelling the request task)."""
    from conftest import make_service

    class HangingCompletions:
        async def create(self, **kwargs):
            await asyncio.Event().wait()

    class HangingClient:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": HangingCompletions()})()

    service, _ = make_service([])
    service.async_client = HangingClient()
    try:
        await asyncio.wait_for(service.handle_user_request_async(
            HOTEL_QUERY, user_id="u-probe-timeout", session_id="c-probe-timeout", conversation_history=[]), timeout=0.5)
        return {"raised": None}
    except (asyncio.TimeoutError, TimeoutError) as exc:
        return {"raised": type(exc).__name__}


async def e11_multi_domain_siblings() -> Dict[str, Any]:
    """A multi-domain request: one domain_graph_dispatch node runs two generic_graph
    instances (hotel, attraction) one after another."""
    prefs = {"location": "Sentosa"}
    return await _confirm_chain("u-probe-multi", MULTI_QUERY, prefs, [intent_payload(MULTI_QUERY, prefs)],
                                CallCounter(), Gate())


EXPERIMENTS: Dict[str, Callable[[], Awaitable[Dict[str, Any]]]] = {
    "E1_routing_only": e1_routing_only,
    "E2_restaurant_only": e2_restaurant_only,
    "E3_service_hotel_chain": e3_service_hotel_chain,
    "E4_itinerary_anchor": e4_itinerary_anchor,
    "E5_concurrent_chains": e5_concurrent_chains,
    "E6a_handoff_inherited_context": e6a_handoff_inherited_context,
    "E6b_handoff_fresh_context": e6b_handoff_fresh_context,
    "E7_domain_failure_caught": e7_domain_failure_caught,
    "E8_routing_failure_uncaught": e8_routing_failure_uncaught,
    "E9_background_cancel": e9_background_cancel,
    "E10_request_timeout": e10_request_timeout,
    "E11_multi_domain_siblings": e11_multi_domain_siblings,
}


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

GRAPH_NAMES = {"LangGraph"}
GRAPH_BY_FIRST_NODE = {
    "intention": "request_orchestrator",
    "domain_classification": "routing_graph",
    "task_started": "task_graph",
    "candidate_gather": "generic_or_restaurant",
    "handoff_node": "probe_parent_graph",
}


def build_runs(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    runs: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        rid = ev["run_id"]
        if ev["event"].endswith("_start"):
            runs[rid] = {"run_id": rid, "name": ev["name"], "parent_run_id": ev["parent_run_id"],
                         "start_ms": ev["t_ms"], "end_ms": None, "status": "open", "error_class": None,
                         "metadata": ev["metadata"], "kind": ev["event"].split("_")[0],
                         "asyncio_task": ev["asyncio_task"]}
        elif rid in runs:
            runs[rid]["end_ms"] = ev["t_ms"]
            if ev["event"].endswith("_error"):
                runs[rid]["status"] = "error"
                runs[rid]["error_class"] = ev.get("error_class")
            else:
                runs[rid]["status"] = "completed"
    return runs


def children_of(runs, rid):
    return sorted((r for r in runs.values() if r["parent_run_id"] == rid), key=lambda r: r["start_ms"])


def first_node(runs, rid) -> Optional[str]:
    return next((c["name"] for c in children_of(runs, rid) if not str(c["name"]).startswith("__")), None)


def identify(graph_run, runs) -> str:
    fn = graph_run.get("first_node") or first_node(runs, graph_run["run_id"])
    label = GRAPH_BY_FIRST_NODE.get(fn, f"unknown({fn})")
    if label == "generic_or_restaurant":
        names = {c["name"] for c in children_of(runs, graph_run["run_id"])}
        label = "restaurant_graph" if "rerank_and_summarize" in names else "generic_graph"
    return label


def graph_rows(runs) -> List[Dict[str, Any]]:
    rows = []
    for g in (r for r in runs.values() if r["name"] in GRAPH_NAMES):
        parent = runs.get(g["parent_run_id"]) if g["parent_run_id"] else None
        grand = runs.get(parent["parent_run_id"]) if parent and parent["parent_run_id"] else None
        rows.append({
            "graph": identify(g, runs),
            "graph_run_id": g["run_id"],
            "status": g["status"],
            "error_class": g["error_class"],
            "parent_run_id": g["parent_run_id"],
            "parent_name": parent["name"] if parent else None,
            "parent_graph": identify(grand, runs) if grand and grand["name"] in GRAPH_NAMES else None,
            "parent_ended_before_child": bool(parent and parent["end_ms"] is not None and g["end_ms"] is not None
                                              and parent["end_ms"] < g["end_ms"]),
            "ends_after_root": bool(g["parent_run_id"] and g["end_ms"] is not None
                                    and (root_of(g, runs)["end_ms"] or 0) < g["end_ms"]),
            "start_ms": g["start_ms"], "end_ms": g["end_ms"],
            "asyncio_task": g["asyncio_task"],
            "thread_id_seen": g["metadata"].get("thread_id"),
            "checkpoint_ns_seen": g["metadata"].get("checkpoint_ns") or g["metadata"].get("langgraph_checkpoint_ns"),
        })
    return sorted(rows, key=lambda r: r["start_ms"])


def root_of(run, runs):
    cur = run
    while cur["parent_run_id"] and cur["parent_run_id"] in runs:
        cur = runs[cur["parent_run_id"]]
    return cur


def integrity(runs) -> Dict[str, Any]:
    dangling = sum(1 for r in runs.values() if r["parent_run_id"] and r["parent_run_id"] not in runs)
    cycles = 0
    for r in runs.values():
        seen, cur = set(), r
        while cur and cur["parent_run_id"]:
            if cur["run_id"] in seen:
                cycles += 1
                break
            seen.add(cur["run_id"])
            cur = runs.get(cur["parent_run_id"])
    return {"dangling_parents": dangling, "cycles": cycles,
            "unmatched_starts": sum(1 for r in runs.values() if r["status"] == "open"),
            "error_runs": sum(1 for r in runs.values() if r["status"] == "error")}


def error_runs(runs) -> List[Dict[str, Any]]:
    out = []
    for r in sorted(runs.values(), key=lambda r: r["start_ms"]):
        if r["status"] != "error":
            continue
        label = identify(r, runs) if r["name"] in GRAPH_NAMES else r["name"]
        out.append({"run": label, "error_class": r["error_class"]})
    return out


def concurrency_leaks(runs) -> Dict[str, Any]:
    """Each task_graph's user (from its task thread) must match the user of the request it hangs under."""
    checked, leaks = 0, []
    for g in graph_rows(runs):
        if g["graph"] != "task_graph" or not g["parent_run_id"]:
            continue
        task_nodes = children_of(runs, g["graph_run_id"])
        root = root_of(runs[g["graph_run_id"]], runs)
        root_nodes = children_of(runs, root["run_id"])
        user = lambda rs: next(((r["metadata"].get("thread_id") or "").split(":")[0] for r in rs if r["metadata"].get("thread_id")), "")
        task_user, root_user = user(task_nodes), user(root_nodes)
        checked += 1
        if task_user and root_user and task_user != root_user:
            leaks.append({"task_user": task_user, "root_user": root_user})
    return {"task_graphs_checked": checked, "cross_request_leaks": leaks}


def analyse(events: List[Dict[str, Any]], outcome: Dict[str, Any]) -> Dict[str, Any]:
    runs = build_runs(events)
    return {
        "outcome": outcome,
        "event_count": len(events),
        "event_types": sorted({e["event"] for e in events}),
        "tool_or_llm_events": sum(1 for e in events if e["event"].split("_")[0] in {"tool", "llm"}),
        "graph_root_run_names": sorted({r["name"] for r in runs.values() if r["name"] in GRAPH_NAMES}),
        "graphs": graph_rows(runs),
        "errors": error_runs(runs),
        "integrity": integrity(runs),
        "concurrency": concurrency_leaks(runs),
    }
