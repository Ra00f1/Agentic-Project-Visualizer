"""Email Triage — mirrors mock_agent's `email_triage` workflow."""

from __future__ import annotations

from apv_trace import trace

from . import simulate_agent

WORKFLOW_NAME = "Email Triage"


@trace(kind="workflow", name=WORKFLOW_NAME)
async def run(fail: bool = False) -> None:
    await simulate_agent("Analytics Coordinator", tools=["shared_audit_log"], duration=0.4)
    await simulate_agent("Analytics Specialist 1", tools=["query_analytics", "run_experiment", "shared_audit_log"], duration=0.7)
    await simulate_agent("Analytics Specialist 2", tools=["run_experiment", "compute_cohort", "shared_audit_log"], duration=0.7)
    await simulate_agent("Analytics Specialist 3", tools=["compute_cohort", "check_order_status", "shared_audit_log"], duration=0.6, fail=fail)
