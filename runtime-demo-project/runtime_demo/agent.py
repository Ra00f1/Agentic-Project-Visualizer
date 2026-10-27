"""TriageAgent — the one agent handler in the runtime demo.

Calls one tool, one plain internal function, and has a deliberate failure
path — a single run exercises every `kind` the trace schema defines
(workflow, agent, tool, function) plus the error phase.
"""

from __future__ import annotations

from opentelemetry import trace

from .functions import normalize_text
from .tools import classify_ticket_tool, lookup_kb_tool

_tracer = trace.get_tracer(__name__)
_HANDLE_ATTRS = {
    "apv.kind": "agent",
    "apv.name": "handle",
    "apv.module": __name__,
    "apv.qualname": "TriageAgent.handle",
}


class TriageAgent:
    """Routes a support ticket to a canned resolution via keyword classification."""

    async def handle(self, ticket_text: str) -> str:
        with _tracer.start_as_current_span("TriageAgent.handle", attributes=_HANDLE_ATTRS):
            if ticket_text.strip().upper() == "CRASH":
                # Deliberate failure path: start_as_current_span's defaults
                # record the exception onto this span (-> an "error" event)
                # and still let it propagate, so run_demo.py's own handling
                # of CRASH is unaffected by the OTel rewrite.
                raise RuntimeError("simulated triage failure")
            normalized = normalize_text(ticket_text)
            category = classify_ticket_tool(normalized)
            articles = lookup_kb_tool(category)
            if not articles:
                return f"No KB articles found for category {category!r}."
            return f"Suggested article: {articles[0]}"
