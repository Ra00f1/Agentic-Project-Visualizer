"""Meeting Notes Generator — mirrors mock_agent's `meeting_notes_generator` workflow."""

from __future__ import annotations

from apv_trace import trace

from . import simulate_agent

WORKFLOW_NAME = "Meeting Notes Generator"


@trace(kind="workflow", name=WORKFLOW_NAME)
async def run(fail: bool = False) -> None:
    await simulate_agent("Kv Coordinator", tools=["shared_audit_log"], duration=0.4)
    await simulate_agent("Kv Specialist 1", tools=["kv_get", "kv_set", "shared_audit_log"], duration=0.7)
    await simulate_agent("Kv Specialist 2", tools=["kv_set", "query_analytics", "shared_audit_log"], duration=0.7, fail=fail)
