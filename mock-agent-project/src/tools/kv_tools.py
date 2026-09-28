"""Kv tools tools — stubs backing the Mongo tool rows."""

from typing import Any

def kv_get(*args: Any, **kwargs: Any) -> dict:
    """Stub for the kv_get tool."""
    return {"tool": "kv_get", "ok": True}

def kv_set(*args: Any, **kwargs: Any) -> dict:
    """Stub for the kv_set tool."""
    return {"tool": "kv_set", "ok": True}

