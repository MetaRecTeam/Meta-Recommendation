"""B5 · One place the debug API gets a trace store from.

Which store is in use is a deployment fact, not an API fact: with
``DATABASE_URL`` set and migration 20261115_0008 applied, traces survive a
restart; otherwise they live in this process. Both answer the same calls and
return the same page, so the route does not care which it has.

The in-memory store is synchronous (A's collector appends to it from
callbacks) and the Postgres store is asynchronous (the rest of the
repository's database access is). ``resolve`` is how the route tolerates both
without a branch at every call site.
"""
from __future__ import annotations

import inspect
import os
from typing import Any, Optional

from .storage import InMemoryTraceStore

_store: Optional[Any] = None


async def resolve(value: Any) -> Any:
    """Return a value that may or may not be awaitable."""
    return await value if inspect.isawaitable(value) else value


def _postgres_store() -> Optional[Any]:
    """A Postgres store if one can be built, else None.

    A missing driver, an unreadable URL or an unmigrated database all mean the
    same thing to a caller: there is no durable store, fall back to memory.
    Tracing is a debug aid and must never be the reason the Debug Arena fails
    to start.
    """
    if not os.getenv("DATABASE_URL"):
        return None
    try:
        from business_db import get_async_sessionmaker

        from .postgres_storage import PostgresTraceStore

        return PostgresTraceStore(get_async_sessionmaker())
    except Exception:
        return None


def get_trace_store() -> Any:
    """The process-wide trace store, built once."""
    global _store
    if _store is None:
        _store = _postgres_store() or InMemoryTraceStore()
    return _store


def reset_trace_store(store: Optional[Any] = None) -> Any:
    """Replace the store. For tests and for start-up wiring."""
    global _store
    _store = store
    return get_trace_store()


def trace_id_for(debug_run_id: str) -> str:
    """One trace per debug run, named from it so either id finds the other."""
    return f"trace-{debug_run_id}"
