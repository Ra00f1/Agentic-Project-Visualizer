"""Content Moderation — mirrors mock_agent's `content_moderation` workflow."""

from __future__ import annotations

from apv_trace import trace

from . import simulate_agent

WORKFLOW_NAME = "Content Moderation"


@trace(kind="workflow", name=WORKFLOW_NAME)
async def run(fail: bool = False) -> None:
    await simulate_agent("Comms Coordinator", tools=["shared_audit_log"], duration=0.4)
    await simulate_agent("Comms Specialist 1", tools=["send_slack_message", "send_teams_message", "shared_audit_log"], duration=0.6)
    await simulate_agent("Comms Specialist 2", tools=["send_teams_message", "book_calendar", "shared_audit_log"], duration=0.6)
    await simulate_agent("Comms Specialist 3", tools=["book_calendar", "upload_to_s3", "shared_audit_log"], duration=0.6, fail=fail)
