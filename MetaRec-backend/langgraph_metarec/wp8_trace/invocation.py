"""The single reviewed path every MetaRec graph invocation goes through.

``invoke_with_wp8_trace`` calls ``graph.ainvoke`` exactly once. With no trace
context, or a disabled one, the call is byte-for-byte what the runner did
before. With tracing on it changes only two config keys, and only for this
graph run:

- ``run_name`` = the ``graph_definition_id``, because every compiled graph
  otherwise reports the callback name ``"LangGraph"``. It does not reach
  nested runs or checkpoint metadata.
- ``callbacks`` gains the WP8 collector, unless the collector is already
  inherited from an enclosing traced run (then the key is left alone, which
  keeps that run as the callback parent). An explicit hand-off parent
  (``TraceContext.handoff``) is applied whatever form the callbacks take.

LangChain replaces, not merges, any config key passed explicitly, so passing
``metadata`` or ``configurable`` to a nested call would drop the inherited checkpoint
namespace and callback parent. WP8 identity travels in the run name and the collector,
and never in config metadata (DECISIONS.md R13).

Tracing never fails or repeats the business call: a graph the helper cannot
trace runs once, untraced, and the collector is told (DECISIONS.md R15).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler, BaseCallbackManager, CallbackManager
from langchain_core.runnables.config import var_child_runnable_config

from .topology import matches_registered

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TraceContext:
    """Explicit WP8 trace context, passed down through every graph runner.

    ``config``, ``sink`` and ``projector`` are Member B's ``GraphTraceConfigV1``,
    ``TraceEventSink`` and ``SafeProjector``; the collector uses them.
    """

    trace_id: str
    debug_run_id: str
    collector: BaseCallbackHandler
    enabled: bool = True
    # The callback run that invoked this graph, carried explicitly across a
    # boundary that does not copy context (e.g. a background-task hand-off).
    # It must be the real run id observed at the call site, never a guess.
    parent_run_id: Optional[UUID] = None
    config: Any = None
    sink: Any = None
    projector: Any = None

    def handoff(self, parent_run_id: UUID) -> "TraceContext":
        return replace(self, parent_run_id=parent_run_id)


def trace_kwargs(trace_ctx: Optional[TraceContext]) -> dict:
    """``{"trace_ctx": ctx}`` while tracing is on, else ``{}``.

    Runners and service methods forward the context with ``**trace_kwargs(ctx)``,
    so with tracing off every call keeps exactly the arguments it had before WP8.
    """
    return {"trace_ctx": trace_ctx} if trace_ctx is not None and trace_ctx.enabled else {}


def _effective_callbacks(config: Optional[Mapping[str, Any]]) -> Any:
    """The callbacks LangChain will use for this call: explicit, else inherited."""
    if config is not None and config.get("callbacks") is not None:
        return config["callbacks"]
    inherited = var_child_runnable_config.get()
    return inherited.get("callbacks") if inherited else None


def _handlers(callbacks: Any) -> list:
    if callbacks is None:
        return []
    if isinstance(callbacks, BaseCallbackManager):
        return list(callbacks.handlers) + list(callbacks.inheritable_handlers)
    return list(callbacks)


def _traced_callbacks(callbacks: Any, ctx: TraceContext) -> Any:
    """Callbacks for a traced call, or None to leave the effective ones untouched.

    Adds the collector unless it is already there, and applies an explicit
    hand-off parent to a list and to a callback manager alike.
    """
    has_collector = ctx.collector in _handlers(callbacks)
    if has_collector and ctx.parent_run_id is None:
        return None
    if isinstance(callbacks, BaseCallbackManager):
        manager = callbacks.copy()
        if not has_collector:
            manager.add_handler(ctx.collector, inherit=True)
        if ctx.parent_run_id is not None:
            manager.parent_run_id = ctx.parent_run_id
        return manager
    handlers = [*(callbacks or [])]
    if not has_collector:
        handlers.append(ctx.collector)
    if ctx.parent_run_id is not None:
        return CallbackManager(handlers=handlers, inheritable_handlers=handlers, parent_run_id=ctx.parent_run_id)
    return handlers


def traced_config(
    config: Optional[Mapping[str, Any]],
    trace_ctx: TraceContext,
    graph_definition_id: str,
) -> dict:
    """A new config for one traced graph run; the caller's config is not mutated."""
    merged = dict(config or {})
    merged["run_name"] = graph_definition_id
    callbacks = _traced_callbacks(_effective_callbacks(config), trace_ctx)
    if callbacks is not None:
        merged["callbacks"] = callbacks
    return merged


def _untraceable_reason(graph: Any, graph_definition_id: str) -> Optional[str]:
    try:
        return None if matches_registered(graph, graph_definition_id) else "topology_mismatch"
    except KeyError:
        return "unregistered_definition"
    except Exception:
        logger.exception("WP8 topology check failed for %s", graph_definition_id)
        return "topology_check_failed"


def _report_untraced(trace_ctx: TraceContext, graph_definition_id: str, reason: str) -> None:
    logger.warning(
        "WP8: %s runs untraced (%s); if its topology changed, bump its version in "
        "langgraph_metarec/wp8_trace/topology.py", graph_definition_id, reason,
    )
    report = getattr(trace_ctx.collector, "on_untraced_graph", None)
    if report is None:
        return
    try:
        report(graph_definition_id, reason)
    except Exception:
        logger.exception("WP8 collector could not record an untraced graph")


async def _plain_call(graph: Any, input: Any, config: Optional[Mapping[str, Any]], kwargs: dict) -> Any:
    if config is None:
        return await graph.ainvoke(input, **kwargs)
    return await graph.ainvoke(input, config, **kwargs)


async def invoke_with_wp8_trace(
    graph: Any,
    input: Any,
    config: Optional[Mapping[str, Any]],
    trace_ctx: Optional[TraceContext],
    *,
    graph_definition_id: str,
    **kwargs: Any,
) -> Any:
    """Invoke a compiled MetaRec graph exactly once, traced when ``trace_ctx`` is enabled."""
    if trace_ctx is None or not trace_ctx.enabled:
        return await _plain_call(graph, input, config, kwargs)
    reason = _untraceable_reason(graph, graph_definition_id)
    if reason is not None:
        _report_untraced(trace_ctx, graph_definition_id, reason)
        return await _plain_call(graph, input, config, kwargs)
    return await graph.ainvoke(input, traced_config(config, trace_ctx, graph_definition_id), **kwargs)
