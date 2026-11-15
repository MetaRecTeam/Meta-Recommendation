"""B5: the trace API, and the promise that turning it on changes nothing else.

The riskiest part of Part B, because it edits live code. Most of this file is
about what must NOT change: a client that has never heard of graph tracing
sends what it sent before and gets what it got before, the start response
gains no field, and with the flag off nothing is stored and every gate
answers with a typed error rather than a 500.
"""
from __future__ import annotations

import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from business_models import AuthSessionPayload, UserRecord, UserRole, UserSessionRecord, utc_now
from langgraph_metarec.wp8_trace.config import default_trace_config
from langgraph_metarec.wp8_trace.service import reset_trace_store, trace_id_for
from langgraph_metarec.wp8_trace.storage import InMemoryTraceStore
from langgraph_metarec.wp8_trace.topology import topology_definitions
from tests.wp8_store_support import draft

pytestmark = pytest.mark.backend_unit

CREATE_URL = "/internal/debug/behavior-tests"
PURGE_URL = "/internal/debug/graph-traces/purge"


def trace_url(run_id: str) -> str:
    return f"/internal/debug/behavior-tests/{run_id}/graph-trace"


def _admin_payload() -> AuthSessionPayload:
    uid = str(uuid.uuid4())
    user = UserRecord(id=uid, kind="registered", role=UserRole.ADMIN,
                      email="admin@example.com", display_name="Acting Admin", status="active")
    session = UserSessionRecord(id=str(uuid.uuid4()), user_id=uid, status="active",
                                expires_at=utc_now() + timedelta(days=30), user=user)
    return AuthSessionPayload(token="admin-token", session=session, user=user)


class StubService:
    """Enough service for the create route to start and finish a run."""

    async def find_task_status_async(self, task_id, user_id=None, session_id=None):
        return None

    async def handle_user_request_async(self, *args, **kwargs) -> Dict[str, Any]:
        return {"status": "completed", "response": {"message": "ok"}}


def build_client(monkeypatch, *, trace_flag: bool, store: Optional[Any] = None):
    monkeypatch.setenv("DEBUG_UI_ENABLED", "1")
    monkeypatch.setenv("DEBUG_GRAPH_TRACE_ENABLED", "1" if trace_flag else "0")
    import internal.debug.router as router_mod

    reset_trace_store(store or InMemoryTraceStore())
    tmp_root = tempfile.mkdtemp()

    class TmpTraceStorage(router_mod.DebugTraceStorage):
        def __init__(self, storage_dir: str = "debug_traces") -> None:
            self.base_dir = Path(tmp_root) / storage_dir
            self.base_dir.mkdir(parents=True, exist_ok=True)
            self._lock = Lock()

    monkeypatch.setattr(router_mod, "DebugTraceStorage", TmpTraceStorage)

    async def require_admin(_request: Request) -> AuthSessionPayload:
        return _admin_payload()

    app = FastAPI()
    app.include_router(router_mod.create_debug_router(lambda: StubService(), require_admin))
    return TestClient(app)


@pytest.fixture
def client_on(monkeypatch):
    with build_client(monkeypatch, trace_flag=True) as client:
        yield client


@pytest.fixture
def client_off(monkeypatch):
    with build_client(monkeypatch, trace_flag=False) as client:
        yield client


def start_run(client, **body) -> str:
    body.setdefault("query", "find me something")
    body.setdefault("max_wait_seconds", 1)
    body.setdefault("poll_interval_ms", 50)
    response = client.post(CREATE_URL, json=body)
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


# --- invariant 5: with the flag off, nothing changes --------------------------

def test_the_start_response_gains_no_field_when_tracing_is_off(client_off):
    body = client_off.post(CREATE_URL, json={"query": "x", "max_wait_seconds": 1}).json()
    assert set(body) == {"ok", "run_id", "status"}


def test_the_start_response_gains_no_field_when_tracing_is_on(client_on):
    body = client_on.post(CREATE_URL, json={"query": "x", "max_wait_seconds": 1}).json()
    assert set(body) == {"ok", "run_id", "status"}


def test_a_client_that_sends_no_trace_config_stores_exactly_what_it_did_before(client_on):
    # The new field is omitted from the stored params, not written as null, so
    # an existing caller's run record is byte-identical to before.
    run_id = start_run(client_on)
    record = client_on.get(f"{CREATE_URL}/{run_id}").json()
    assert "graph_trace_config" not in record["config"]


def test_a_client_that_sends_a_trace_config_has_it_recorded(client_on):
    config = default_trace_config().model_dump(mode="json")
    run_id = start_run(client_on, graph_trace_config=config)
    record = client_on.get(f"{CREATE_URL}/{run_id}").json()
    assert record["config"]["graph_trace_config"]["schema_version"] == "graph-trace-config/1.0"


def test_with_the_flag_off_nothing_is_stored(client_off):
    run_id = start_run(client_off)
    from langgraph_metarec.wp8_trace.service import get_trace_store
    assert get_trace_store().exists(trace_id_for(run_id)) is False


def test_with_the_flag_off_a_trace_config_is_refused_not_ignored(client_off):
    # Silently ignoring it would leave a caller believing it was traced.
    response = client_off.post(CREATE_URL, json={
        "query": "x", "max_wait_seconds": 1,
        "graph_trace_config": default_trace_config().model_dump(mode="json")})
    assert response.status_code == 400
    assert response.json()["detail"] == "graph_trace_disabled"


def test_with_the_flag_off_the_read_route_is_a_typed_400_not_a_500(client_off):
    run_id = start_run(client_off)
    response = client_off.get(trace_url(run_id))
    assert response.status_code == 400
    assert response.json()["detail"] == "graph_trace_disabled"


# --- the capture request -----------------------------------------------------

def test_a_malformed_trace_config_is_rejected_before_the_run_starts(client_on):
    bad = default_trace_config().model_dump(mode="json")
    bad["retention_days"] = 99
    response = client_on.post(CREATE_URL, json={
        "query": "x", "max_wait_seconds": 1, "graph_trace_config": bad})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_trace_config"


def test_an_unregistered_redaction_policy_is_rejected_by_name(client_on):
    bad = default_trace_config().model_dump(mode="json")
    bad["redaction_policy_id"] = "permissive-v9"
    response = client_on.post(CREATE_URL, json={
        "query": "x", "max_wait_seconds": 1, "graph_trace_config": bad})
    assert response.status_code == 400
    assert response.json()["detail"] == "unknown_redaction_policy"


def test_a_rejected_config_starts_no_run(client_on):
    before = client_on.get(CREATE_URL).json()
    bad = default_trace_config().model_dump(mode="json")
    bad["retention_days"] = 0
    client_on.post(CREATE_URL, json={"query": "x", "graph_trace_config": bad})
    assert client_on.get(CREATE_URL).json() == before


def test_a_config_with_enabled_false_captures_nothing(client_on):
    config = default_trace_config(enabled=False).model_dump(mode="json")
    run_id = start_run(client_on, graph_trace_config=config)
    from langgraph_metarec.wp8_trace.service import get_trace_store
    assert get_trace_store().exists(trace_id_for(run_id)) is False


# --- reading a trace ---------------------------------------------------------

def test_a_traced_run_serves_a_page(client_on):
    run_id = start_run(client_on, graph_trace_config=default_trace_config().model_dump(mode="json"))
    response = client_on.get(trace_url(run_id))
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["schema_version"] == "graph-trace/1.0"
    assert page["debug_run_id"] == run_id


def test_the_page_carries_the_applied_config_not_the_requested_one(client_on):
    requested = default_trace_config().model_dump(mode="json")
    requested["limits"]["max_events"] = 10
    run_id = start_run(client_on, graph_trace_config=requested)
    snapshot = client_on.get(trace_url(run_id)).json()["config_snapshot"]
    assert snapshot["limits"]["max_events"] == 10


def test_the_page_carries_the_graph_topology(client_on):
    run_id = start_run(client_on, graph_trace_config=default_trace_config().model_dump(mode="json"))
    page = client_on.get(trace_url(run_id)).json()
    served = {d["graph_definition_id"] for d in page["topology"]["definitions"]}
    assert served == {d.graph_definition_id for d in topology_definitions()}


def test_a_run_that_was_never_traced_says_so(client_on):
    run_id = start_run(client_on)  # no capture request
    response = client_on.get(trace_url(run_id))
    assert response.status_code == 404
    assert response.json()["detail"] == "trace_unavailable"


def test_an_unknown_run_is_a_404(client_on):
    response = client_on.get(trace_url("no-such-run"))
    assert response.status_code == 404
    assert response.json()["detail"] == "Debug run not found"


def test_the_trace_is_sealed_when_the_run_finishes(client_on):
    run_id = start_run(client_on, graph_trace_config=default_trace_config().model_dump(mode="json"))
    assert client_on.get(trace_url(run_id)).json()["status"] != "running"


# --- paging ------------------------------------------------------------------

def test_the_cursor_walks_a_populated_trace(client_on):
    from langgraph_metarec.wp8_trace.service import get_trace_store
    run_id = start_run(client_on, graph_trace_config=default_trace_config().model_dump(mode="json"))
    # The run starts as a background task, so the trace is opened a tick after
    # the POST returns. Reading one page waits for that.
    assert client_on.get(trace_url(run_id)).status_code == 200
    store = get_trace_store()
    trace_id = trace_id_for(run_id)
    for _ in range(12):
        store.append(trace_id, draft(trace_id=trace_id, debug_run_id=run_id))

    seen, cursor = [], 0
    while True:
        page = client_on.get(trace_url(run_id), params={"after_sequence": cursor, "limit": 5}).json()
        seen.extend(e["sequence"] for e in page["events"])
        cursor = page["event_window"]["next_after_sequence"]
        if not page["event_window"]["has_more"]:
            break
    assert seen == list(range(1, 13))


@pytest.mark.parametrize("params,expected", [
    ({"limit": 0}, 422),
    ({"limit": 10_000}, 422),
    ({"after_sequence": -1}, 422),
])
def test_out_of_range_paging_arguments_are_refused(client_on, params, expected):
    run_id = start_run(client_on, graph_trace_config=default_trace_config().model_dump(mode="json"))
    assert client_on.get(trace_url(run_id), params=params).status_code == expected


# --- delete and purge --------------------------------------------------------

def test_a_trace_can_be_deleted_ahead_of_retention(client_on):
    run_id = start_run(client_on, graph_trace_config=default_trace_config().model_dump(mode="json"))
    assert client_on.delete(trace_url(run_id)).status_code == 200
    assert client_on.get(trace_url(run_id)).json()["detail"] == "trace_unavailable"


def test_deleting_a_trace_that_does_not_exist_is_a_404(client_on):
    run_id = start_run(client_on)
    assert client_on.delete(trace_url(run_id)).status_code == 404


def test_delete_is_refused_when_the_flag_is_off(client_off):
    run_id = start_run(client_off)
    response = client_off.delete(trace_url(run_id))
    assert response.status_code == 400
    assert response.json()["detail"] == "graph_trace_disabled"


def test_purge_reports_how_many_traces_went(client_on):
    response = client_on.post(PURGE_URL)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "purged": 0}


def test_purge_is_refused_when_the_flag_is_off(client_off):
    assert client_off.post(PURGE_URL).status_code == 400


# --- the arena gate ----------------------------------------------------------

def test_every_trace_route_is_invisible_when_the_debug_arena_is_off(monkeypatch):
    monkeypatch.setenv("DEBUG_GRAPH_TRACE_ENABLED", "1")
    with build_client(monkeypatch, trace_flag=True) as client:
        monkeypatch.setenv("DEBUG_UI_ENABLED", "0")
    # The arena flag is read when the router is built, so build one with it off.
    monkeypatch.setenv("DEBUG_UI_ENABLED", "0")
    import internal.debug.router as router_mod

    async def require_admin(_request: Request) -> AuthSessionPayload:
        return _admin_payload()

    app = FastAPI()
    app.include_router(router_mod.create_debug_router(lambda: StubService(), require_admin))
    with TestClient(app) as client:
        assert client.get(trace_url("any")).status_code == 404
        assert client.delete(trace_url("any")).status_code == 404
        assert client.post(PURGE_URL).status_code == 404
