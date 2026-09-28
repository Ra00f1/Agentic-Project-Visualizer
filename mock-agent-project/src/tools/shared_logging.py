"""Shared audit-log tool called from ~15 agents. Wide fan-in target."""

from typing import Any


def audit_log(event: str, payload: Any = None) -> None:
    """Append `event` to the audit log. Stub."""
    # This is deliberately called from many places to stress fan-in
    # rendering. See `shared_audit_log` in the Mongo tools collection.
    _sink = (event, payload)
    del _sink
