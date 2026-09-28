"""Comms tools tools — stubs backing the Mongo tool rows."""

from typing import Any

def send_slack(*args: Any, **kwargs: Any) -> dict:
    """Stub for the send_slack tool."""
    return {"tool": "send_slack", "ok": True}

def send_teams(*args: Any, **kwargs: Any) -> dict:
    """Stub for the send_teams tool."""
    return {"tool": "send_teams", "ok": True}

def book_calendar(*args: Any, **kwargs: Any) -> dict:
    """Stub for the book_calendar tool."""
    return {"tool": "book_calendar", "ok": True}

