"""Customer tools tools — stubs backing the Mongo tool rows."""

from typing import Any

def check_order_status(*args: Any, **kwargs: Any) -> dict:
    """Stub for the check_order_status tool."""
    return {"tool": "check_order_status", "ok": True}

def issue_refund(*args: Any, **kwargs: Any) -> dict:
    """Stub for the issue_refund tool."""
    return {"tool": "issue_refund", "ok": True}

def lookup_customer(*args: Any, **kwargs: Any) -> dict:
    """Stub for the lookup_customer tool."""
    return {"tool": "lookup_customer", "ok": True}

def escalate_to_human(*args: Any, **kwargs: Any) -> dict:
    """Stub for the escalate_to_human tool."""
    return {"tool": "escalate_to_human", "ok": True}

