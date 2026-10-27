"""Fraud Detection — mirrors mock_agent's `fraud_detection` workflow."""

from __future__ import annotations

from apv_trace import trace

from . import simulate_agent

WORKFLOW_NAME = "Fraud Detection"


@trace(kind="workflow", name=WORKFLOW_NAME)
async def run(fail: bool = False) -> None:
    await simulate_agent("Security Coordinator", tools=["shared_audit_log"], duration=0.4)
    await simulate_agent("Security Specialist 1", tools=["scan_vulnerabilities", "rotate_secret", "audit_permissions", "shared_audit_log"], duration=0.8)
    await simulate_agent("Security Specialist 2", tools=["rotate_secret", "audit_permissions", "shared_audit_log"], duration=0.6)
    await simulate_agent("Security Specialist 3", tools=["audit_permissions", "send_slack_message", "shared_audit_log"], duration=0.6, fail=fail)
