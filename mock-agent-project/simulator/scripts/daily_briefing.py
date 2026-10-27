"""Daily Briefing — mirrors mock_agent's `daily_briefing` workflow."""

from __future__ import annotations

from apv_trace import trace

from . import simulate_agent

WORKFLOW_NAME = "Daily Briefing"


# `simulate_agent` splits `duration` evenly across `tools` (see
# scripts/__init__.py), so each call below passes `2.0 * len(tools)` --
# every individual tool step in this workflow takes exactly 2 seconds,
# unlike the other scripts' agent-level durations tuned for a faster demo.
@trace(kind="workflow", name=WORKFLOW_NAME)
async def run(fail: bool = False) -> None:
    await simulate_agent("Observability Coordinator", tools=["shared_audit_log"], duration=2.0)
    await simulate_agent("Observability Specialist 1", tools=["query_metrics", "query_traces", "query_logs", "shared_audit_log"], duration=8.0)
    await simulate_agent("Observability Specialist 2", tools=["query_traces", "query_logs", "scan_vulnerabilities", "shared_audit_log"], duration=8.0)
    await simulate_agent("Observability Specialist 3", tools=["query_logs", "scan_vulnerabilities", "rotate_secret", "shared_audit_log"], duration=8.0, fail=fail)
