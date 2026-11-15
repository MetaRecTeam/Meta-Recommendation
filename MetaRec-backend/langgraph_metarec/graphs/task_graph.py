from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from langgraph_metarec.checkpointing import RuntimeCheckpointer, task_thread_id
from langgraph_metarec.itinerary_snapshot import sanitize_planning_snapshot
from langgraph_metarec.state import GraphRuntimeState, ProgressEvent, RuntimeErrorRecord, TaskStatusProjection
from langgraph_metarec.wp8_trace.invocation import TraceContext, invoke_with_wp8_trace


ProgressCallback = Callable[[Dict[str, Any]], Awaitable[None]]
DomainRunner = Callable[[ProgressCallback], Awaitable[Dict[str, Any]]]
ProjectionWriter = Callable[[Dict[str, Any]], Awaitable[None]]


@dataclass
class TaskGraphAdapters:
    run_domain_graph: DomainRunner
    write_projection: ProjectionWriter


class TaskGraphState(TypedDict, total=False):
    runtime: Dict[str, Any]


def _projection(runtime: GraphRuntimeState, *, result_object: Any = None) -> Dict[str, Any]:
    task_status = runtime.task_status or TaskStatusProjection(task_id=runtime.task_id)
    payload = task_status.model_dump(mode="json")
    payload["task_id"] = runtime.task_id
    payload["user_id"] = runtime.user_id
    payload["conversation_id"] = runtime.conversation_id or "default"
    payload["metadata"] = {
        **payload.get("metadata", {}),
        "thread_id": runtime.thread_id,
        "task_thread_id": runtime.task_thread_id,
        "branch_id": runtime.branch_id,
        "progress_events": [event.model_dump(mode="json") for event in runtime.progress_events],
        "result_metadata": (
            runtime.domain_graph_result.metadata
            if runtime.domain_graph_result is not None
            else {}
        ),
    }
    if result_object is not None:
        payload["result"] = result_object
    return payload


def build_task_graph(adapters: TaskGraphAdapters, *, checkpointer: Any):
    async def task_started(state: TaskGraphState) -> TaskGraphState:
        runtime = GraphRuntimeState.from_checkpoint(state.get("runtime"))
        event = ProgressEvent(stage="task_started", progress=0, message="Task started")
        runtime.progress_events.append(event)
        runtime.task_status = TaskStatusProjection(
            task_id=runtime.task_id,
            status="processing",
            progress=0,
            message="Task started",
            metadata={"stage": "task_started"},
        )
        await adapters.write_projection(_projection(runtime))
        return {"runtime": runtime.to_checkpoint()}

    async def domain_graph_dispatch(state: TaskGraphState) -> TaskGraphState:
        runtime = GraphRuntimeState.from_checkpoint(state.get("runtime"))

        async def progress_callback(event_payload: Dict[str, Any]) -> None:
            event = ProgressEvent(
                stage=event_payload.get("stage"),
                status=event_payload.get("status", "processing"),
                progress=int(event_payload.get("progress", 0)),
                message=event_payload.get("message", ""),
                metadata={
                    key: value
                    for key, value in event_payload.items()
                    if key not in {"stage", "status", "progress", "message", "planning_snapshot"}
                },
            )
            runtime.progress_events.append(event)
            previous_metadata = (
                runtime.task_status.metadata if runtime.task_status is not None else {}
            )
            previous_snapshot = previous_metadata.get("planning_snapshot")
            try:
                previous_revision = int((previous_snapshot or {}).get("revision") or 0)
            except (TypeError, ValueError, AttributeError):
                previous_revision = 0
            snapshot = sanitize_planning_snapshot(
                event_payload.get("planning_snapshot"),
                previous_revision=previous_revision,
            )
            runtime.task_status = TaskStatusProjection(
                task_id=runtime.task_id,
                status="processing" if event.status != "error" else "error",
                progress=event.progress,
                message=event.message,
                metadata={
                    "stage": event.stage,
                    **({"planning_snapshot": snapshot} if snapshot is not None else (
                        {"planning_snapshot": previous_snapshot}
                        if isinstance(previous_snapshot, dict) else {}
                    )),
                },
            )
            await adapters.write_projection(_projection(runtime))

        try:
            domain_result = await adapters.run_domain_graph(progress_callback)
            runtime.domain_graph_result = domain_result["domain_graph_result"]
            result_payload = domain_result.get("result_payload") or {}
            result_metadata = (
                result_payload.get("metadata") if isinstance(result_payload, dict) else {}
            ) or {}
            is_itinerary = (
                getattr(runtime.domain_graph_result, "domain", None) == "itinerary"
                or result_metadata.get("domain") == "itinerary"
                or isinstance(result_metadata.get("itinerary"), dict)
            )
            runtime.task_status = TaskStatusProjection(
                task_id=runtime.task_id,
                status="completed",
                progress=100,
                message=(
                    "Itinerary information gathered."
                    if is_itinerary else "Recommendations ready!"
                ),
                result=result_payload,
                metadata=domain_result.get("metadata", {}),
            )
            await adapters.write_projection(_projection(runtime, result_object=domain_result["result_object"]))
        except Exception as exc:
            runtime.errors.append(RuntimeErrorRecord(message=str(exc), node="domain_graph_dispatch"))
            runtime.task_status = TaskStatusProjection(
                task_id=runtime.task_id,
                status="error",
                progress=(runtime.task_status.progress if runtime.task_status else 0),
                message=str(exc),
                error=str(exc),
            )
            await adapters.write_projection(_projection(runtime))
        return {"runtime": runtime.to_checkpoint()}

    def result_projection(state: TaskGraphState) -> TaskGraphState:
        return state

    graph = StateGraph(TaskGraphState)
    graph.add_node("task_started", task_started)
    graph.add_node("domain_graph_dispatch", domain_graph_dispatch)
    graph.add_node("result_projection", result_projection)
    graph.add_edge(START, "task_started")
    graph.add_edge("task_started", "domain_graph_dispatch")
    graph.add_edge("domain_graph_dispatch", "result_projection")
    graph.add_edge("result_projection", END)
    return graph.compile(checkpointer=checkpointer)


async def run_task_graph(
    *,
    adapters: TaskGraphAdapters,
    user_id: str,
    conversation_id: Optional[str],
    branch_id: Optional[str],
    task_id: str,
    query: str,
    checkpointer: Optional[Any] = None,
    trace_ctx: Optional[TraceContext] = None,
) -> GraphRuntimeState:
    owner = None
    if checkpointer is None:
        owner = RuntimeCheckpointer()
        active_checkpointer = await owner.aget()
    else:
        active_checkpointer = checkpointer
    graph = build_task_graph(adapters, checkpointer=active_checkpointer)
    task_tid = task_thread_id(user_id, conversation_id, branch_id, task_id)
    config = {"configurable": {"thread_id": task_tid}}
    try:
        stored = await graph.aget_state(config)
        stored_runtime = stored.values.get("runtime") if stored and stored.values else None
        runtime = GraphRuntimeState.from_checkpoint(stored_runtime)
        runtime.user_id = user_id
        runtime.conversation_id = conversation_id
        runtime.branch_id = branch_id
        runtime.task_id = task_id
        runtime.task_thread_id = task_tid
        runtime.query = query
        final_state = await invoke_with_wp8_trace(
            graph, {"runtime": runtime.to_checkpoint()}, config, trace_ctx, graph_definition_id="task-graph@v1"
        )
        return GraphRuntimeState.from_checkpoint(final_state["runtime"])
    finally:
        if owner is not None:
            await owner.aclose()
