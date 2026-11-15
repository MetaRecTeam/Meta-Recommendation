from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from langgraph_metarec.wp8_trace.config import (
    TraceConfigError,
    graph_trace_enabled as graph_trace_flag,
    validate_trace_config,
)
from langgraph_metarec.wp8_trace.models import (
    DEFAULT_PAGE_EVENTS,
    MAX_PAGE_EVENTS,
    GraphTraceV1,
)
from langgraph_metarec.wp8_trace.service import get_trace_store, resolve, trace_id_for
from langgraph_metarec.wp8_trace.storage import TraceNotFound
from langgraph_metarec.wp8_trace.topology import topology_definitions

from .unit_registry import UnitSpec, register_default_debug_units

from business_models import AuthSessionPayload
from llm_compat import create_chat_completion_async

try:
    from llm_service import client as debug_llm_client, LLM_MODEL as DEBUG_LLM_MODEL
except Exception:
    debug_llm_client = None
    DEBUG_LLM_MODEL = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


logger = logging.getLogger("metarec.debug.wp8")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return _serialize(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return _serialize(value.dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            return _serialize(vars(value))
        except Exception:
            pass
    return str(value)


_SENSITIVE_PARTS = ("token", "api_key", "apikey", "authorization", "cookie", "secret", "password")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, val in value.items():
            if any(p in str(key).lower() for p in _SENSITIVE_PARTS):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = _sanitize(val)
        return out
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "...<truncated>"
    return _serialize(value)


class DebugTraceStorage:
    def __init__(self, storage_dir: str = "debug_traces"):
        # Keep trace storage at backend root (MetaRec-backend/debug_traces)
        # even though this module now lives under internal/debug/.
        self.base_dir = Path(__file__).resolve().parents[2] / storage_dir
        self.base_dir.mkdir(exist_ok=True)
        self._lock = Lock()

    def _path(self, run_id: str) -> Path:
        return self.base_dir / f"{run_id}.json"

    def create_run(self, kind: str, config: Dict[str, Any]) -> Dict[str, Any]:
        run_id = str(uuid.uuid4())
        record = {
            "id": run_id,
            "kind": kind,
            "status": "queued",
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
            "config": _sanitize(config),
            "events": [],
            "artifacts": {},
            "explanation": None,
            "error": None,
        }
        self.save(record)
        return record

    def load(self, run_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(run_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def save(self, record: Dict[str, Any]) -> None:
        with self._lock:
            record["updated_at"] = _utc_now_iso()
            with open(self._path(record["id"]), "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)

    def update(self, run_id: str, **fields: Any) -> Dict[str, Any]:
        record = self.load(run_id)
        if not record:
            raise FileNotFoundError(run_id)
        record.update(_serialize(fields))
        self.save(record)
        return record

    def append_event(
        self,
        run_id: str,
        *,
        event_type: str,
        label: str,
        status: str = "info",
        data: Any = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        record = self.load(run_id)
        if not record:
            raise FileNotFoundError(run_id)
        record.setdefault("events", []).append(
            {
                "timestamp": _utc_now_iso(),
                "type": event_type,
                "label": label,
                "status": status,
                "duration_ms": duration_ms,
                "data": _sanitize(data),
            }
        )
        self.save(record)

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        files = sorted(self.base_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out: List[Dict[str, Any]] = []
        for path in files[:limit]:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    record = json.load(f)
                out.append(
                    {
                        "id": record.get("id"),
                        "kind": record.get("kind"),
                        "status": record.get("status"),
                        "created_at": record.get("created_at"),
                        "updated_at": record.get("updated_at"),
                        "event_count": len(record.get("events", [])),
                        "error": record.get("error"),
                    }
                )
            except Exception:
                continue
        return out


class DebugRateLimiter:
    """
    Simple in-memory sliding-window rate limiter keyed by session/action.
    Suitable for internal debug endpoints only (process-local state).
    """
    def __init__(self):
        self._events: Dict[str, List[float]] = {}
        self._lock = Lock()

    def allow(self, key: str, *, limit: int, window_seconds: int) -> Tuple[bool, int]:
        limit = max(1, int(limit))
        window_seconds = max(1, int(window_seconds))
        now = time.monotonic()
        with self._lock:
            events = [ts for ts in self._events.get(key, []) if (now - ts) < window_seconds]
            if len(events) >= limit:
                retry_after = max(1, int(window_seconds - (now - events[0])) + 1)
                self._events[key] = events
                return False, retry_after
            events.append(now)
            self._events[key] = events
            return True, 0


class DebugConfig(BaseModel):
    enabled: bool
    llm_explain_enabled: bool
    auth_mode: str
    cookie_name: str


class BehaviorTestCreateRequest(BaseModel):
    query: str = Field(..., min_length=1)
    conversation_id: Optional[str] = None
    use_online_agent: bool = False
    auto_confirm: bool = True
    confirm_message: str = "Yes, that's correct"
    max_wait_seconds: int = 90
    poll_interval_ms: int = 500
    # WP8. Optional with a default, so a client that has never heard of graph
    # tracing sends exactly what it sent before and gets exactly what it got
    # before. Omitted means "no preference"; the server flag still decides
    # whether anything is captured at all.
    graph_trace_config: Optional[Dict[str, Any]] = None


class BehaviorTrackRequest(BaseModel):
    # user_id/conversation_id are optional *narrowing filters*, not lookup keys:
    # task_id alone identifies the task. Omit them to track by pasted id alone.
    task_id: str = Field(..., min_length=1)
    user_id: Optional[str] = None
    conversation_id: Optional[str] = None
    max_wait_seconds: int = 90
    poll_interval_ms: int = 500


class ExplainRequest(BaseModel):
    mode: str = "nl_explain"


class UnitRunRequest(BaseModel):
    unit_name: str
    input_data: Optional[Dict[str, Any]] = None
    input_mode: str = "manual"  # manual | sample | schema | llm
    use_llm_generation: bool = False


class UnitInputGenerateRequest(BaseModel):
    unit_name: str
    mode: str = "schema"  # schema | sample | llm


class ApiPlaygroundInputGenerateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mode: str = "schema"  # schema | llm
    json_schema: Dict[str, Any] = Field(alias="schema")
    method: Optional[str] = None
    path: Optional[str] = None
    summary: Optional[str] = None


class UnitRegistry:
    def __init__(self, service_getter: Callable[[], Any]):
        self._service_getter = service_getter
        self._specs: Dict[str, UnitSpec] = {}
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        register_default_debug_units(self, service_getter)

    def register(self, spec: UnitSpec, handler: Callable[[Dict[str, Any]], Any]) -> None:
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def list_specs(self) -> List[Dict[str, Any]]:
        return [spec.model_dump() for spec in self._specs.values()]

    def get_spec(self, name: str) -> UnitSpec:
        spec = self._specs.get(name)
        if not spec:
            raise KeyError(name)
        return spec

    async def run(self, name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        handler = self._handlers[name]
        started = time.perf_counter()
        try:
            result = handler(payload)
            if inspect.isawaitable(result):
                result = await result  # type: ignore[assignment]
            return {"ok": True, "duration_ms": int((time.perf_counter() - started) * 1000), "output": _sanitize(result)}
        except Exception as exc:
            return {
                "ok": False,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }


def _validate_schema(data: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    errors: List[str] = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(data, dict):
            return [f"{path}: expected object"]
        for k in schema.get("required", []):
            if k not in data:
                errors.append(f"{path}.{k}: missing required field")
        props = schema.get("properties", {})
        for k, child in props.items():
            if k in data and isinstance(child, dict):
                errors.extend(_validate_schema(data[k], child, f"{path}.{k}"))
        return errors
    if t == "array":
        if not isinstance(data, list):
            return [f"{path}: expected array"]
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(data[:5]):
                errors.extend(_validate_schema(item, item_schema, f"{path}[{i}]"))
        return errors
    if t == "string":
        if not isinstance(data, str):
            return [f"{path}: expected string"]
        min_len = schema.get("minLength")
        if isinstance(min_len, int) and len(data) < min_len:
            errors.append(f"{path}: too short")
        return errors
    if t == "integer" and (not isinstance(data, int) or isinstance(data, bool)):
        return [f"{path}: expected integer"]
    if t == "number" and (not isinstance(data, (int, float)) or isinstance(data, bool)):
        return [f"{path}: expected number"]
    if t == "boolean" and not isinstance(data, bool):
        return [f"{path}: expected boolean"]
    return errors


def _generate_from_schema(schema: Dict[str, Any]) -> Any:
    if "example" in schema:
        return schema["example"]
    t = schema.get("type")
    if t == "object":
        props = schema.get("properties", {})
        req = schema.get("required", [])
        obj: Dict[str, Any] = {}
        for k in req:
            if isinstance(props.get(k), dict):
                obj[k] = _generate_from_schema(props[k])
        return obj
    if t == "array":
        child = schema.get("items", {"type": "string"})
        return [_generate_from_schema(child if isinstance(child, dict) else {"type": "string"})]
    if t == "string":
        if schema.get("enum"):
            return schema["enum"][0]
        return "test"
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return False
    return None


async def _generate_llm_input(unit_spec: UnitSpec) -> Optional[Dict[str, Any]]:
    return await _generate_llm_json_from_schema(
        unit_spec.input_schema,
        context_hint="Restaurant recommender debug unit test input",
    )


async def _generate_llm_json_from_schema(schema: Dict[str, Any], context_hint: str = "") -> Optional[Dict[str, Any]]:
    if debug_llm_client is None or not DEBUG_LLM_MODEL:
        return None
    context_line = f"Context: {context_hint}\n" if context_hint else ""
    prompt = (
        "Generate one JSON object that strictly satisfies the JSON schema.\n"
        "Use realistic but dummy values. Prefer compact payloads.\n"
        f"{context_line}"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    try:
        resp = await create_chat_completion_async(
            debug_llm_client,
            model=DEBUG_LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=1.0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content if resp and resp.choices else ""
        parsed = json.loads(content) if isinstance(content, str) else None
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


async def _generate_unit_input(spec: UnitSpec, mode: str) -> Dict[str, Any]:
    if mode == "sample":
        return spec.sample_input
    if mode == "llm":
        generated = await _generate_llm_input(spec)
        if generated is not None:
            return generated
    return _generate_from_schema(spec.input_schema)


async def _generate_api_playground_input(payload: ApiPlaygroundInputGenerateRequest) -> Dict[str, Any]:
    if payload.mode == "llm":
        hint_parts = [p for p in [payload.method, payload.path, payload.summary] if p]
        generated = await _generate_llm_json_from_schema(payload.json_schema, context_hint="API Playground " + " ".join(hint_parts))
        if generated is not None:
            return generated
    return _generate_from_schema(payload.json_schema)


def _debug_conversation_id(run_id: str) -> str:
    # A fresh UUID conversation scope for a behavior run (tasks/results have no
    # FK to conversations, so this need not be a real conversation row).
    return str(uuid.uuid4())


def _get_confirmation_message(response_obj: Any) -> str:
    """
    Support both dict payloads and Pydantic models for confirmation_request.
    """
    if response_obj is None:
        return ""
    if isinstance(response_obj, dict):
        confirmation = response_obj.get("confirmation_request")
        if isinstance(confirmation, dict):
            return str(confirmation.get("message", "") or "")
        if confirmation is not None:
            return str(getattr(confirmation, "message", "") or "")
        return ""
    confirmation = getattr(response_obj, "confirmation_request", None)
    if confirmation is None:
        return ""
    if isinstance(confirmation, dict):
        return str(confirmation.get("message", "") or "")
    return str(getattr(confirmation, "message", "") or "")


def create_debug_router(
    service_getter: Callable[[], Any],
    require_admin: Callable[[Request], Any],
) -> APIRouter:
    router = APIRouter(prefix="/internal/debug", tags=["internal-debug"])
    trace_storage = DebugTraceStorage()
    rate_limiter = DebugRateLimiter()
    unit_registry = UnitRegistry(service_getter)
    jobs: Dict[str, asyncio.Task] = {}

    debug_enabled = _env_flag("DEBUG_UI_ENABLED", False)
    explain_enabled = _env_flag("DEBUG_LLM_EXPLAIN_ENABLED", True)
    # WP8 graph tracing. Off by default: with it off no trace is persisted and
    # no existing response gains a field (WP8 brief, invariant 5). The flag is
    # read through wp8_trace.config so the router and the library can never
    # disagree about what "on" means.
    graph_trace_enabled = graph_trace_flag()
    trace_store = get_trace_store()
    # Auth is delegated to the app's real-user session via require_admin; the
    # arena no longer owns a token/session. cookie_name is reported by /config
    # so the frontend knows which app session cookie carries auth.
    cookie_name = os.getenv("METAREC_SESSION_COOKIE_NAME", "metarec_session")
    debug_exec_timeout_seconds = max(1, int(os.getenv("DEBUG_EXEC_TIMEOUT_SECONDS", "120")))
    llm_gen_rate_limit_count = max(1, int(os.getenv("DEBUG_LLM_GEN_RATE_LIMIT_COUNT", "10")))
    llm_gen_rate_limit_window_seconds = max(1, int(os.getenv("DEBUG_LLM_GEN_RATE_LIMIT_WINDOW_SECONDS", "60")))
    llm_explain_rate_limit_count = max(1, int(os.getenv("DEBUG_LLM_EXPLAIN_RATE_LIMIT_COUNT", "3")))
    llm_explain_rate_limit_window_seconds = max(1, int(os.getenv("DEBUG_LLM_EXPLAIN_RATE_LIMIT_WINDOW_SECONDS", "300")))

    def require_enabled() -> None:
        if not debug_enabled:
            raise HTTPException(status_code=404, detail="Debug UI is disabled")

    async def require_auth(request: Request) -> AuthSessionPayload:
        # Gate on DEBUG_UI_ENABLED, then on a real authenticated user with the
        # ADMIN role (resolved by the app's require_admin dependency).
        require_enabled()
        return await require_admin(request)

    def enforce_rate_limit(session: AuthSessionPayload, *, action: str, limit: int, window_seconds: int) -> None:
        session_id = session.user.id
        allowed, retry_after = rate_limiter.allow(
            f"{session_id}:{action}",
            limit=limit,
            window_seconds=window_seconds,
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded for {action}. Retry after {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )

    async def with_debug_timeout(coro: Any, *, label: str):
        try:
            return await asyncio.wait_for(coro, timeout=debug_exec_timeout_seconds)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail=f"{label} timed out after {debug_exec_timeout_seconds}s")

    async def run_job_with_timeout(run_id: str, job_coro: Any, *, label: str) -> None:
        try:
            await asyncio.wait_for(job_coro, timeout=debug_exec_timeout_seconds)
        except asyncio.TimeoutError:
            trace_storage.append_event(
                run_id,
                event_type="timeout",
                label=f"{label} timed out",
                status="error",
                data={"timeout_seconds": debug_exec_timeout_seconds},
            )
            trace_storage.update(run_id, status="timeout", error=f"{label} timed out after {debug_exec_timeout_seconds}s")
        except Exception as exc:
            trace_storage.append_event(
                run_id,
                event_type="debug_job",
                label=f"{label} failed",
                status="error",
                data={"error": str(exc), "traceback": traceback.format_exc()},
            )
            trace_storage.update(run_id, status="error", error=str(exc))

    def record_artifact(run_id: str, key: str, value: Any) -> None:
        rec = trace_storage.load(run_id)
        if not rec:
            return
        rec.setdefault("artifacts", {})[key] = _sanitize(value)
        trace_storage.save(rec)

    async def poll_task(
        run_id: str,
        task_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        max_wait_seconds: int,
        poll_interval_ms: int,
    ) -> Dict[str, Any]:
        service = service_getter()
        deadline = time.monotonic() + max(1, max_wait_seconds)
        last_sig: Optional[str] = None
        while time.monotonic() < deadline:
            # find_* rather than get_*: the tracker may hold only a task id. With
            # both scope values supplied (the behavior-create path) this narrows to
            # exactly the row get_task_status_async would have returned.
            status = await service.find_task_status_async(task_id, user_id, session_id)
            if status is None:
                trace_storage.append_event(run_id, event_type="task_status", label="Task not found", status="warning", data={"task_id": task_id})
            else:
                safe = _serialize(status)
                sig = json.dumps(
                    {
                        "status": safe.get("status"),
                        "progress": safe.get("progress"),
                        "message": safe.get("message"),
                        "stage": safe.get("stage"),
                        "stage_number": safe.get("stage_number"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if sig != last_sig:
                    last_sig = sig
                    trace_storage.append_event(
                        run_id,
                        event_type="task_status",
                        label=f"Task status: {safe.get('status', 'unknown')}",
                        data=safe,
                    )
                if safe.get("status") in {"completed", "error"}:
                    record_artifact(run_id, "task_status_final", safe)
                    return safe
            await asyncio.sleep(max(0.1, poll_interval_ms / 1000.0))
        timeout_status = {"status": "timeout", "task_id": task_id, "message": f"Timed out after {max_wait_seconds}s"}
        trace_storage.append_event(run_id, event_type="task_status", label="Task tracking timeout", status="error", data=timeout_status)
        record_artifact(run_id, "task_status_final", timeout_status)
        return timeout_status

    def trace_requested(req: BehaviorTestCreateRequest):
        """The config for this run, or None when nothing should be captured.

        Three gates, all of which must be open: the Debug Arena exists, the
        server flag is on, and this run asked. A request cannot open the
        second one.
        """
        if not graph_trace_enabled or req.graph_trace_config is None:
            return None
        try:
            config = validate_trace_config(req.graph_trace_config)
        except TraceConfigError:
            return None
        return config if config.enabled else None

    async def open_trace(run_id: str, req: BehaviorTestCreateRequest) -> None:
        config = trace_requested(req)
        if config is None:
            return
        try:
            await resolve(trace_store.start_trace(
                trace_id_for(run_id), run_id,
                config=config, definitions=topology_definitions(),
            ))
        except Exception as exc:  # tracing must never fail a debug run
            logger.warning("wp8: could not open trace for run %s: %s", run_id, exc)

    async def close_trace(run_id: str, req: BehaviorTestCreateRequest) -> None:
        if trace_requested(req) is None:
            return
        rec = trace_storage.load(run_id) or {}
        status = {
            "completed": "completed",
            "error": "failed",
            "timeout": "cancelled",
        }.get(rec.get("status"), "incomplete")
        try:
            await resolve(trace_store.finalize(trace_id_for(run_id), status))
        except Exception as exc:
            logger.warning("wp8: could not finalize trace for run %s: %s", run_id, exc)

    async def run_behavior_create(run_id: str, req: BehaviorTestCreateRequest, owner_user_id: str) -> None:
        await open_trace(run_id, req)
        service = service_getter()
        # Run as the authenticated admin's real user so tasks persist through the
        # normal Postgres path (users FK satisfied). Conversation scope is a fresh
        # id when unspecified, avoiding clutter in the admin's chat list.
        user_id = owner_user_id
        session_id = req.conversation_id or _debug_conversation_id(run_id)
        try:
            trace_storage.update(run_id, status="running")
            trace_storage.append_event(
                run_id,
                event_type="behavior_test",
                label="Run started",
                data={
                    "query": req.query,
                    "user_id": user_id,
                    "session_id": session_id,
                    "use_online_agent": req.use_online_agent,
                    "auto_confirm": req.auto_confirm,
                },
            )
            t0 = time.perf_counter()
            initial = await service.handle_user_request_async(
                req.query,
                user_id=user_id,
                conversation_history=None,
                session_id=session_id,
                use_online_agent=req.use_online_agent,
            )
            trace_storage.append_event(
                run_id,
                event_type="service_call",
                label="Initial handle_user_request_async",
                status="completed",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                data=initial,
            )
            record_artifact(run_id, "initial_response", initial)
            current = initial

            if current.get("type") == "confirmation" and req.auto_confirm:
                t1 = time.perf_counter()
                confirmation_message = _get_confirmation_message(current)
                confirm_resp = await service.handle_user_request_async(
                    req.confirm_message,
                    user_id=user_id,
                    conversation_history=[
                        {"role": "user", "content": req.query},
                        {"role": "assistant", "content": confirmation_message},
                    ],
                    session_id=session_id,
                    use_online_agent=req.use_online_agent,
                )
                trace_storage.append_event(
                    run_id,
                    event_type="service_call",
                    label="Auto-confirm follow-up",
                    status="completed",
                    duration_ms=int((time.perf_counter() - t1) * 1000),
                    data={"confirm_message": req.confirm_message, "response": confirm_resp},
                )
                record_artifact(run_id, "auto_confirm_response", confirm_resp)
                current = confirm_resp

            if current.get("type") == "task_created":
                task_id = current.get("task_id")
                record_artifact(run_id, "task_created", {"task_id": task_id})
                if task_id:
                    final_status = await poll_task(
                        run_id, task_id, user_id, session_id, req.max_wait_seconds, req.poll_interval_ms
                    )
                    if final_status.get("status") == "completed":
                        trace_storage.update(run_id, status="completed")
                    elif final_status.get("status") == "timeout":
                        trace_storage.update(run_id, status="timeout", error=final_status.get("message"))
                    else:
                        trace_storage.update(run_id, status="error", error=final_status.get("error") or final_status.get("message"))
                else:
                    trace_storage.update(run_id, status="error", error="task_created without task_id")
            else:
                record_artifact(run_id, "behavior_test_result", {"final_response": current})
                trace_storage.update(run_id, status="completed")
        except Exception as exc:
            trace_storage.append_event(
                run_id,
                event_type="behavior_test",
                label="Run failed",
                status="error",
                data={"error": str(exc), "traceback": traceback.format_exc()},
            )
            trace_storage.update(run_id, status="error", error=str(exc))
        finally:
            # Sealed however the run ended, including a cancellation, so a
            # poller is never left watching a trace that says "running" for
            # ever (R11's finish condition).
            await close_trace(run_id, req)

    async def run_behavior_track(run_id: str, req: BehaviorTrackRequest) -> None:
        try:
            trace_storage.update(run_id, status="running")
            final_status = await poll_task(
                run_id,
                req.task_id,
                req.user_id,
                req.conversation_id,
                req.max_wait_seconds,
                req.poll_interval_ms,
            )
            if final_status.get("status") == "completed":
                trace_storage.update(run_id, status="completed")
            elif final_status.get("status") == "timeout":
                trace_storage.update(run_id, status="timeout", error=final_status.get("message"))
            else:
                trace_storage.update(run_id, status="error", error=final_status.get("error") or final_status.get("message"))
        except Exception as exc:
            trace_storage.append_event(run_id, event_type="task_status", label="Tracker failed", status="error", data={"error": str(exc)})
            trace_storage.update(run_id, status="error", error=str(exc))

    async def explain_trace(run_id: str) -> Dict[str, Any]:
        rec = trace_storage.load(run_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Debug run not found")
        if debug_llm_client is None or not DEBUG_LLM_MODEL:
            raise HTTPException(status_code=400, detail="LLM client unavailable")
        prompt = (
            "Explain this debug trace step-by-step for engineers. "
            "Label observed facts vs inferred causes, and give optimization suggestions.\n\n"
            f"{json.dumps(_sanitize(rec), ensure_ascii=False, indent=2)[:120000]}"
        )
        started = time.perf_counter()
        resp = await create_chat_completion_async(
            debug_llm_client,
            model=DEBUG_LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        content = (resp.choices[0].message.content if resp and resp.choices else "") or ""
        explanation = {
            "generated_at": _utc_now_iso(),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "content": content.strip(),
        }
        rec["explanation"] = explanation
        trace_storage.save(rec)
        trace_storage.append_event(run_id, event_type="llm_explain", label="Generated NL explanation", status="completed", data={"duration_ms": explanation["duration_ms"]})
        return explanation

    @router.get("/config")
    async def get_config():
        return DebugConfig(
            enabled=debug_enabled,
            llm_explain_enabled=bool(debug_enabled and explain_enabled and debug_llm_client and DEBUG_LLM_MODEL),
            auth_mode="user_role",
            cookie_name=cookie_name,
        )

    # NOTE: identity/session verification moved to /api/admin/session (admin
    # router), which is independent of DEBUG_UI_ENABLED. The old
    # /internal/debug/session endpoint was removed as legacy.

    @router.get("/behavior-tests")
    async def list_behavior(_: Dict[str, Any] = Depends(require_auth)):
        return {"runs": trace_storage.list_runs()}

    @router.post("/behavior-tests")
    async def start_behavior(req: BehaviorTestCreateRequest, session: AuthSessionPayload = Depends(require_auth)):
        req.max_wait_seconds = min(req.max_wait_seconds, debug_exec_timeout_seconds)
        owner_user_id = session.user.id
        # Reject a malformed capture request before any work starts, with the
        # reason code rather than a 500 from deeper in.
        if req.graph_trace_config is not None:
            if not graph_trace_enabled:
                raise HTTPException(status_code=400, detail="graph_trace_disabled")
            try:
                validate_trace_config(req.graph_trace_config)
            except TraceConfigError as exc:
                raise HTTPException(status_code=400, detail=exc.code) from None
        # A caller who sent no capture request stores exactly the fields it
        # stored before: the new key is left out rather than written as null.
        params = req.model_dump(exclude={"graph_trace_config"})
        if req.graph_trace_config is not None:
            params["graph_trace_config"] = req.graph_trace_config
        rec = trace_storage.create_run("behavior_create", {**params, "owner_user_id": owner_user_id})
        jobs[rec["id"]] = asyncio.create_task(
            run_job_with_timeout(rec["id"], run_behavior_create(rec["id"], req, owner_user_id), label="Behavior create run")
        )
        return {"ok": True, "run_id": rec["id"], "status": rec["status"]}

    @router.post("/behavior-tests/track")
    async def start_track(req: BehaviorTrackRequest, _: AuthSessionPayload = Depends(require_auth)):
        # Preflight existence check: do not create a debug tracking run for a
        # non-existent task. user_id/conversation_id are optional narrowing filters,
        # not required keys -- the chat only surfaces a task id (Chat.tsx's "Task ID"
        # copy button), and a task id is globally unique on its own. require_auth
        # gates this on the ADMIN role, which is what authorizes the wider lookup.
        existing = await service_getter().find_task_status_async(
            req.task_id, req.user_id, req.conversation_id
        )
        if existing is None:
            raise HTTPException(status_code=404, detail="Task ID not found; no tracking run created")
        req.max_wait_seconds = min(req.max_wait_seconds, debug_exec_timeout_seconds)
        # Record the scope the id resolved to so the trace shows whose task this is
        # even when the operator only pasted an id. Not fed back into req: the
        # projection reports conversation_id as "default" for rows stored with NULL,
        # which would not match as a filter on the next poll.
        rec = trace_storage.create_run(
            "behavior_track",
            {
                **req.model_dump(),
                "resolved_user_id": existing.get("user_id"),
                "resolved_conversation_id": existing.get("conversation_id"),
            },
        )
        jobs[rec["id"]] = asyncio.create_task(
            run_job_with_timeout(rec["id"], run_behavior_track(rec["id"], req), label="Behavior track run")
        )
        return {"ok": True, "run_id": rec["id"], "status": rec["status"]}

    @router.get("/behavior-tests/{run_id}")
    async def get_behavior(run_id: str, _: Dict[str, Any] = Depends(require_auth)):
        rec = trace_storage.load(run_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Debug run not found")
        job = jobs.get(run_id)
        if job and job.done():
            jobs.pop(run_id, None)
        rec["job_running"] = bool(job and not job.done())
        return rec

    def require_graph_trace() -> None:
        """Both gates, in the order a caller can act on.

        The Debug Arena being off is a 404: the route does not exist for you.
        Tracing being off is a typed 400, never a 500, so a UI can tell
        "turned off" from "broken".
        """
        require_enabled()
        if not graph_trace_enabled:
            raise HTTPException(status_code=400, detail="graph_trace_disabled")

    def require_run(run_id: str) -> None:
        if not trace_storage.load(run_id):
            raise HTTPException(status_code=404, detail="Debug run not found")

    @router.get("/behavior-tests/{run_id}/graph-trace", response_model=GraphTraceV1)
    async def get_graph_trace(
        run_id: str,
        after_sequence: int = Query(0, ge=0),
        limit: int = Query(DEFAULT_PAGE_EVENTS, ge=1, le=MAX_PAGE_EVENTS),
        _: AuthSessionPayload = Depends(require_auth),
    ):
        """One page of the graph trace for a debug run.

        ``after_sequence`` is a cursor, not an offset: pass back the
        ``next_after_sequence`` from the previous page. The same shape serves
        an in-progress run and a finished one, so a poller calls this
        unchanged from start to end.
        """
        require_graph_trace()
        require_run(run_id)
        try:
            return await resolve(
                trace_store.read_page(
                    trace_id_for(run_id), after_sequence=after_sequence, limit=limit
                )
            )
        except TraceNotFound:
            # The run exists but was never traced: it started before tracing
            # was on, or it did not ask. A trace cannot be reconstructed from
            # the coarse task events after the fact, so say so rather than
            # invent a path.
            raise HTTPException(status_code=404, detail="trace_unavailable") from None

    @router.delete("/behavior-tests/{run_id}/graph-trace")
    async def delete_graph_trace(
        run_id: str,
        _: AuthSessionPayload = Depends(require_auth),
    ):
        """Delete one trace ahead of its retention. Its events go with it."""
        require_graph_trace()
        require_run(run_id)
        deleted = await resolve(trace_store.delete(trace_id_for(run_id)))
        if not deleted:
            raise HTTPException(status_code=404, detail="trace_unavailable")
        return {"ok": True, "run_id": run_id, "deleted": True}

    @router.post("/graph-traces/purge")
    async def purge_graph_traces(_: AuthSessionPayload = Depends(require_auth)):
        """Drop every trace past its retention.

        Retention is a promise, so something has to keep it. This is the
        manual trigger; a scheduled caller hits the same route.
        """
        require_graph_trace()
        purged = await resolve(trace_store.purge_expired())
        return {"ok": True, "purged": purged}

    @router.post("/behavior-tests/{run_id}/explain")
    async def explain_endpoint(run_id: str, payload: ExplainRequest, session: AuthSessionPayload = Depends(require_auth)):
        require_enabled()
        if not explain_enabled:
            raise HTTPException(status_code=400, detail="LLM explanation disabled")
        enforce_rate_limit(
            session,
            action="llm_explain",
            limit=llm_explain_rate_limit_count,
            window_seconds=llm_explain_rate_limit_window_seconds,
        )
        try:
            explanation = await with_debug_timeout(explain_trace(run_id), label="LLM explanation")
            return {"ok": True, "mode": payload.mode, "explanation": explanation}
        except HTTPException:
            raise
        except Exception as exc:
            trace_storage.append_event(run_id, event_type="llm_explain", label="LLM explanation failed", status="error", data={"error": str(exc)})
            raise HTTPException(status_code=500, detail="LLM explanation failed")

    @router.get("/unit-tests/units")
    async def list_units(_: Dict[str, Any] = Depends(require_auth)):
        return {"units": unit_registry.list_specs()}

    @router.post("/unit-tests/generate-input")
    async def generate_unit_input(payload: UnitInputGenerateRequest, session: AuthSessionPayload = Depends(require_auth)):
        try:
            spec = unit_registry.get_spec(payload.unit_name)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unit not found")
        if payload.mode == "llm":
            enforce_rate_limit(
                session,
                action="llm_generate_unit_input",
                limit=llm_gen_rate_limit_count,
                window_seconds=llm_gen_rate_limit_window_seconds,
            )
        generated = await with_debug_timeout(_generate_unit_input(spec, payload.mode), label="Unit input generation")
        return {
            "ok": True,
            "unit": spec.name,
            "mode": payload.mode,
            "input_data": _sanitize(generated),
            "validation_errors": _validate_schema(generated, spec.input_schema),
        }

    @router.post("/unit-tests/run")
    async def run_unit(payload: UnitRunRequest, session: AuthSessionPayload = Depends(require_auth)):
        try:
            spec = unit_registry.get_spec(payload.unit_name)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unit not found")
        input_mode = payload.input_mode
        input_data = payload.input_data
        if input_data is None or input_mode in {"sample", "schema"} or payload.use_llm_generation:
            input_mode = "llm" if payload.use_llm_generation else input_mode
            if input_mode == "llm":
                enforce_rate_limit(
                    session,
                    action="llm_generate_unit_input",
                    limit=llm_gen_rate_limit_count,
                    window_seconds=llm_gen_rate_limit_window_seconds,
                )
            input_data = await with_debug_timeout(_generate_unit_input(spec, input_mode), label="Unit input generation")
        if not isinstance(input_data, dict):
            raise HTTPException(status_code=400, detail="input_data must be an object")
        return {
            "ok": True,
            "unit": spec.model_dump(),
            "input_source": input_mode,
            "input_data": _sanitize(input_data),
            "validation_errors": _validate_schema(input_data, spec.input_schema),
            "result": await with_debug_timeout(unit_registry.run(spec.name, input_data), label="Unit test execution"),
        }

    @router.post("/api-playground/generate-input")
    async def generate_api_playground_input(payload: ApiPlaygroundInputGenerateRequest, session: AuthSessionPayload = Depends(require_auth)):
        if payload.mode not in {"schema", "llm"}:
            raise HTTPException(status_code=400, detail="mode must be 'schema' or 'llm'")
        if payload.mode == "llm":
            enforce_rate_limit(
                session,
                action="llm_generate_api_input",
                limit=llm_gen_rate_limit_count,
                window_seconds=llm_gen_rate_limit_window_seconds,
            )
        generated = await with_debug_timeout(_generate_api_playground_input(payload), label="API playground input generation")
        return {
            "ok": True,
            "mode": payload.mode,
            "input_data": _sanitize(generated),
            "validation_errors": _validate_schema(generated, payload.json_schema),
        }

    return router
