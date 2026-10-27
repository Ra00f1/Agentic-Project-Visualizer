"""Workflow entry point for the runtime demo.

The workflow span is the outermost span of a run — the node a viewer would
expect to glow first (and longest) in the visualizer's runtime overlay.
"""

from __future__ import annotations

from opentelemetry import trace

from .agent import TriageAgent

_tracer = trace.get_tracer(__name__)
_WORKFLOW_ATTRS = {
    "apv.kind": "workflow",
    "apv.name": "run_support_ticket_workflow",
    "apv.module": __name__,
    "apv.qualname": "run_support_ticket_workflow",
}

_agent = TriageAgent()


async def run_support_ticket_workflow(ticket_text: str) -> str:
    """Route one support ticket through triage and return its resolution."""
    with _tracer.start_as_current_span("run_support_ticket_workflow", attributes=_WORKFLOW_ATTRS):
        return await _agent.handle(ticket_text)
