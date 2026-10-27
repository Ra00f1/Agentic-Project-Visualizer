"""Multilingual Translation — mirrors mock_agent's `multilingual_translation` workflow."""

from __future__ import annotations

from apv_trace import trace

from . import simulate_agent

WORKFLOW_NAME = "Multilingual Translation"


@trace(kind="workflow", name=WORKFLOW_NAME)
async def run(fail: bool = False) -> None:
    await simulate_agent("I18n Coordinator", tools=["shared_audit_log"], duration=0.4)
    await simulate_agent("I18n Specialist 1", tools=["translate_text", "detect_language", "shared_audit_log"], duration=0.7)
    await simulate_agent("I18n Specialist 2", tools=["detect_language", "classify_sentiment", "shared_audit_log"], duration=0.7, fail=fail)
