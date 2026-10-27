"""Sentiment Analysis Workflow — mirrors mock_agent's `sentiment_analysis_workflow` workflow."""

from __future__ import annotations

from apv_trace import trace

from . import simulate_agent

WORKFLOW_NAME = "Sentiment Analysis Workflow"


@trace(kind="workflow", name=WORKFLOW_NAME)
async def run(fail: bool = False) -> None:
    await simulate_agent("Nlp Coordinator", tools=["shared_audit_log"], duration=0.4)
    await simulate_agent("Nlp Specialist 1", tools=["classify_sentiment", "extract_entities", "shared_audit_log"], duration=0.7)
    await simulate_agent("Nlp Specialist 2", tools=["extract_entities", "score_toxicity", "shared_audit_log"], duration=0.7)
    await simulate_agent("Nlp Specialist 3", tools=["score_toxicity", "query_metrics", "shared_audit_log"], duration=0.6, fail=fail)
