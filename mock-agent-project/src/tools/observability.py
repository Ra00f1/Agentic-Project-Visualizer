"""Observability tools — stubs backing the Mongo tool rows."""

from typing import Any

def query_metrics(*args: Any, **kwargs: Any) -> dict:
    """Stub for the query_metrics tool."""
    return {"tool": "query_metrics", "ok": True}

def query_traces(*args: Any, **kwargs: Any) -> dict:
    """Stub for the query_traces tool."""
    return {"tool": "query_traces", "ok": True}

def query_logs(*args: Any, **kwargs: Any) -> dict:
    """Stub for the query_logs tool."""
    return {"tool": "query_logs", "ok": True}

