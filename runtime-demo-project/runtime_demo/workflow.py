"""Workflow entry point for the runtime demo.

`@trace(kind="workflow")` makes this the outermost span of a run — the node
a viewer would expect to glow first (and longest) in the visualizer's
runtime overlay.
"""

from __future__ import annotations

from apv_trace import trace

from .agent import TriageAgent

_agent = TriageAgent()


@trace(kind="workflow")
async def run_support_ticket_workflow(ticket_text: str) -> str:
    """Route one support ticket through triage and return its resolution."""
    return await _agent.handle(ticket_text)
