"""Code Review Pipeline — mirrors mock_agent's `code_review_pipeline` workflow."""

from __future__ import annotations

from apv_trace import trace

from . import simulate_agent

WORKFLOW_NAME = "Code Review Pipeline"


@trace(kind="workflow", name=WORKFLOW_NAME)
async def run(fail: bool = False) -> None:
    await simulate_agent("Code_review Coordinator", tools=["shared_audit_log"], duration=0.4)
    await simulate_agent("Code_review Specialist 1", tools=["run_lint", "run_tests", "open_pr", "code_diff", "shared_audit_log"], duration=0.8)
    await simulate_agent("Code_review Specialist 2", tools=["run_tests", "open_pr", "code_diff", "shared_audit_log"], duration=0.7)
    await simulate_agent("Code_review Specialist 3", tools=["open_pr", "code_diff", "shared_audit_log"], duration=0.5)
    await simulate_agent("Code_review Specialist 4", tools=["code_diff", "translate_text", "shared_audit_log"], duration=0.5, fail=fail)
