"""The WP8 callback collector: LangChain callbacks -> GraphRunEventV1 drafts.

One ``TraceCollector`` per trace, carried in ``TraceContext`` and attached to
each traced graph run by ``invoke_with_wp8_trace``. It observes; it never
changes a run, and a failure inside it is recorded, not raised.

How a callback run is classified (D0 baseline, DECISIONS.md R11-R15):

- **graph**: its name is a registered ``graph_definition_id`` (the helper sets
  ``run_name``). A turn root has no parent and is ``request-orchestrator@v1``
  in an unsealed trace; a nested graph's parent is a tracked node run.
- **node**: a run directly under a tracked graph whose name is one of that
  graph's topology nodes.
- **tool / llm**: a native tool or LLM callback under a node; recorded as a
  metadata-only child span of exactly that node.
- **pass-through**: edge routers (edge evaluations, not node visits) and any
  other runnable inside a node. They emit nothing; their children attach to
  the enclosing node, following the observed callback chain.
- **withheld** (R12): a run whose parent never started in this trace, a
  parentless graph that is not a turn root, a graph that runs untraced under
  the default name ``"LangGraph"`` (R15), and everything under any of them.
  Every withheld event is counted in ``dropped_event_count``; nothing is
  reparented.

Raw inputs, outputs and exceptions go only to the ``SafeProjector``; drafts
carry only what it returns.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

from langchain_core.callbacks import BaseCallbackHandler

from .interfaces import AppendResult, DraftEvent, Projection, SafeProjector, TraceEventSink
from .invocation import TraceContext
from .models import GraphInstance
from .stubs import NullProjector
from .topology import END_NODE, START_NODE, graph_definition_ids, registered_graph

logger = logging.getLogger(__name__)

TURN_ROOT_DEFINITION = "request-orchestrator@v1"
UNTRACED_GRAPH_NAME = "LangGraph"
_SPANS = frozenset({"tool", "llm"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_instance_id() -> str:
    return f"gi-{uuid.uuid4().hex}"


def _id(value: Any) -> Optional[str]:
    return None if value is None else str(value)


@dataclass
class _Run:
    kind: str  # graph | node | tool | llm
    run_id: str
    parent_run_id: Optional[str]
    graph_definition_id: str
    graph_instance_id: str
    parent_graph_instance_id: Optional[str]
    node_id: Optional[str]
    started: float
    node_input: Any = None
    open: bool = True
    instance_opened: bool = False

    @property
    def runnable_type(self) -> str:
        return {"graph": "graph", "node": "graph_node"}.get(self.kind, self.kind)


class TraceCollector(BaseCallbackHandler):
    # Handle callbacks in order on the caller's thread, never in a thread pool,
    # and never let a collector failure reach the business run.
    run_inline = True
    raise_error = False

    def __init__(
        self,
        trace_id: str,
        debug_run_id: str,
        *,
        sink: TraceEventSink,
        projector: SafeProjector,
        clock: Callable[[], float] = time.perf_counter,
        wall_clock: Callable[[], datetime] = _utc_now,
        new_instance_id: Callable[[], str] = _new_instance_id,
        turn_roots: FrozenSet[str] = frozenset({TURN_ROOT_DEFINITION}),
    ) -> None:
        self.trace_id = trace_id
        self.debug_run_id = debug_run_id
        self.sink = sink
        self.projector = projector
        self._fallback = NullProjector(getattr(projector, "policy_id", NullProjector().policy_id))
        self._clock = clock
        self._wall_clock = wall_clock
        self._new_instance_id = new_instance_id
        self._turn_roots = turn_roots
        self._t0 = clock()
        self._lock = threading.RLock()
        self._runs: Dict[str, _Run] = {}
        self._passthrough: Dict[str, str] = {}  # router / inner runnable -> enclosing tracked run
        self._withheld: Set[str] = set()
        self.dropped_event_count = 0
        self.orphan_run_ids: List[str] = []
        self.unnamed_graph_run_ids: List[str] = []  # graphs seen as "LangGraph": not via the helper's traced path
        self.untraced_graphs: List[Tuple[str, str]] = []
        self.internal_errors: List[str] = []
        self.sealed = False

    # -- signals from the helper and the debug route ---------------------------

    def seal(self) -> None:
        """No further turns will start (DECISIONS.md H7, R11)."""
        with self._lock:
            self.sealed = True

    def on_untraced_graph(self, graph_definition_id: str, reason: str) -> None:
        """The helper ran a graph untraced (R15)."""
        with self._lock:
            self.untraced_graphs.append((graph_definition_id, reason))

    @property
    def open_run_ids(self) -> List[str]:
        with self._lock:
            return [run.run_id for run in self._runs.values() if run.open]

    @property
    def incomplete_reasons(self) -> List[str]:
        """Why this trace cannot finish ``completed`` whatever happens next (R8)."""
        with self._lock:
            reasons = []
            if self.orphan_run_ids:
                reasons.append("orphan")
            if self.untraced_graphs or self.unnamed_graph_run_ids:
                reasons.append("untraced_graph")
            if self.internal_errors:
                reasons.append("collector_error")
            return reasons

    # -- LangChain callbacks ---------------------------------------------------

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        name = kwargs.get("name") or (serialized or {}).get("name")
        self._guard(self._chain_start, _id(run_id), _id(parent_run_id), name, inputs)

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._guard(self._end, _id(run_id), outputs, None)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._guard(self._end, _id(run_id), None, error)

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        self._guard(self._span_start, "tool", _id(run_id), _id(parent_run_id),
                    kwargs.get("name") or (serialized or {}).get("name"))

    def on_tool_end(self, output, *, run_id, **kwargs):
        self._guard(self._end, _id(run_id), None, None)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._guard(self._end, _id(run_id), None, error)

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs):
        self._guard(self._span_start, "llm", _id(run_id), _id(parent_run_id),
                    kwargs.get("name") or (serialized or {}).get("name"))

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        self._guard(self._span_start, "llm", _id(run_id), _id(parent_run_id),
                    kwargs.get("name") or (serialized or {}).get("name"))

    def on_llm_end(self, response, *, run_id, **kwargs):
        self._guard(self._end, _id(run_id), None, None)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._guard(self._end, _id(run_id), None, error)

    # -- classification --------------------------------------------------------

    def _guard(self, handler: Callable[..., Any], *args: Any) -> None:
        with self._lock:
            try:
                handler(*args)
            except Exception as exc:  # noqa: BLE001 — observation must never break the run
                self.internal_errors.append(f"{handler.__name__}: {type(exc).__name__}")
                logger.debug("WP8 collector failed in %s", handler.__name__, exc_info=True)

    def _owner(self, parent_run_id: Optional[str]) -> Tuple[Optional[str], Optional[_Run]]:
        """The tracked graph or node a new run belongs to, through pass-through runs and spans."""
        while parent_run_id in self._passthrough:
            parent_run_id = self._passthrough[parent_run_id]
        owner = self._runs.get(parent_run_id) if parent_run_id is not None else None
        while owner is not None and owner.kind in _SPANS:
            owner = self._runs.get(owner.parent_run_id)
        return parent_run_id, owner

    def _chain_start(self, run_id: str, parent_run_id: Optional[str], name: Optional[str], inputs: Any) -> None:
        parent_run_id, owner = self._owner(parent_run_id)
        if parent_run_id in self._withheld:
            return self._withhold(run_id)
        if name in _registered_ids():
            if parent_run_id is None and name in self._turn_roots and not self.sealed:
                return self._graph_start(run_id, name, None, inputs)
            if owner is not None and owner.kind == "node":
                return self._graph_start(run_id, name, owner, inputs)
            return self._withhold(run_id, orphan=True)
        if owner is None:
            return self._withhold(run_id, orphan=True)
        if owner.kind == "graph" and name in _node_ids(owner.graph_definition_id):
            return self._node_start(run_id, owner, name, inputs)
        if owner.kind == "node" and name == UNTRACED_GRAPH_NAME:
            self.unnamed_graph_run_ids.append(run_id)
            return self._withhold(run_id)
        # Edge routers and any other runnable: no event; children attach to the owner.
        self._passthrough[run_id] = owner.run_id

    def _withhold(self, run_id: str, *, orphan: bool = False) -> None:
        self._withheld.add(run_id)
        self.dropped_event_count += 1
        if orphan:
            self.orphan_run_ids.append(run_id)

    # -- lifecycles ------------------------------------------------------------

    def _graph_start(self, run_id: str, graph_definition_id: str, parent: Optional[_Run], inputs: Any) -> None:
        run = _Run(
            kind="graph", run_id=run_id,
            parent_run_id=parent.run_id if parent else None,
            graph_definition_id=graph_definition_id,
            graph_instance_id=self._new_instance_id(),
            parent_graph_instance_id=parent.graph_instance_id if parent else None,
            node_id=None, started=self._clock(),
        )
        self._runs[run_id] = run
        result = self._emit(run, "graph_start", "running", node_input=inputs)
        if result.sequence is not None:
            self.sink.open_instance(self.trace_id, GraphInstance(
                graph_instance_id=run.graph_instance_id,
                graph_definition_id=graph_definition_id,
                parent_graph_instance_id=run.parent_graph_instance_id,
                parent_node_run_id=run.parent_run_id,
                namespace=[],
                started_sequence=result.sequence,
                ended_sequence=None,
            ))
            run.instance_opened = True

    def _node_start(self, run_id: str, graph: _Run, node_id: str, inputs: Any) -> None:
        run = _Run(
            kind="node", run_id=run_id, parent_run_id=graph.run_id,
            graph_definition_id=graph.graph_definition_id, graph_instance_id=graph.graph_instance_id,
            parent_graph_instance_id=None, node_id=node_id, started=self._clock(), node_input=inputs,
        )
        self._runs[run_id] = run
        self._emit(run, "node_start", "running", node_input=inputs)

    def _span_start(self, kind: str, run_id: str, parent_run_id: Optional[str], name: Optional[str]) -> None:
        parent_run_id, owner = self._owner(parent_run_id)
        if parent_run_id in self._withheld:
            return self._withhold(run_id)
        if owner is None or owner.kind != "node":
            # Never attach a child span to a graph root or a guessed node.
            return self._withhold(run_id, orphan=True)
        run = _Run(
            kind=kind, run_id=run_id, parent_run_id=owner.run_id,
            graph_definition_id=owner.graph_definition_id, graph_instance_id=owner.graph_instance_id,
            parent_graph_instance_id=None, node_id=owner.node_id, started=self._clock(),
        )
        self._runs[run_id] = run
        self._emit(run, f"{kind}_start", "running", raw_name=name)

    def _end(self, run_id: str, outputs: Any, error: Optional[BaseException]) -> None:
        if run_id in self._withheld:
            self.dropped_event_count += 1
            return
        run = self._runs.get(run_id)
        if run is None or not run.open:
            return  # a pass-through run, or a run this trace never started
        duration_ms = round((self._clock() - run.started) * 1000, 3)
        if error is None:
            result = self._emit(run, f"{run.kind}_end", "completed", duration_ms=duration_ms,
                                node_input=run.node_input if run.kind == "node" else None,
                                output=outputs if run.kind in ("graph", "node") else None)
        else:
            status = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
            result = self._emit(run, "error", status, duration_ms=duration_ms, error=error)
        run.open = False
        run.node_input = None
        if run.instance_opened and result.sequence is not None:
            self.sink.close_instance(self.trace_id, run.graph_instance_id, result.sequence)

    def _emit(self, run: _Run, event_type: str, status: str, *, duration_ms: Optional[float] = None,
              node_input: Any = None, output: Any = None, error: Optional[BaseException] = None,
              raw_name: Optional[str] = None) -> AppendResult:
        projection = self._project(
            event_type=event_type, runnable_type=run.runnable_type, graph_definition_id=run.graph_definition_id,
            node_id=run.node_id, node_input=node_input, output=output, error=error, raw_name=raw_name,
        )
        return self.sink.append(self.trace_id, DraftEvent(
            trace_id=self.trace_id,
            debug_run_id=self.debug_run_id,
            timestamp_utc=self._wall_clock(),
            monotonic_offset_ms=round(max(self._clock() - self._t0, 0.0) * 1000, 3),
            event_type=event_type,
            status=status,
            graph_definition_id=run.graph_definition_id,
            graph_instance_id=run.graph_instance_id,
            parent_graph_instance_id=run.parent_graph_instance_id,
            node_id=run.node_id,
            run_id=run.run_id,
            parent_run_id=run.parent_run_id,
            runnable_type=run.runnable_type,
            duration_ms=duration_ms,
            policy_id=self.projector.policy_id,
            projection=projection,
        ))

    def _project(self, **request: Any) -> Projection:
        try:
            return self.projector.project(**request)
        except Exception as exc:  # noqa: BLE001 — fall back to nothing but a generic safe error
            self.internal_errors.append(f"projector: {type(exc).__name__}")
            logger.debug("WP8 projector failed", exc_info=True)
            return self._fallback.project(**request)


def _registered_ids() -> FrozenSet[str]:
    return frozenset(graph_definition_ids())


def _node_ids(graph_definition_id: str) -> FrozenSet[str]:
    nodes = registered_graph(graph_definition_id).definition.nodes
    return frozenset(n.node_id for n in nodes if n.node_id not in (START_NODE, END_NODE))


def new_trace_context(
    trace_id: str,
    debug_run_id: str,
    *,
    sink: TraceEventSink,
    projector: SafeProjector,
    config: Any = None,
    **collector_options: Any,
) -> TraceContext:
    """A TraceContext whose collector writes to ``sink`` through ``projector``."""
    collector = TraceCollector(trace_id, debug_run_id, sink=sink, projector=projector, **collector_options)
    return TraceContext(trace_id=trace_id, debug_run_id=debug_run_id, collector=collector,
                        config=config, sink=sink, projector=projector)
