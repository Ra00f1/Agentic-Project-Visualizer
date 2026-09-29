"""TriageAgent — the one agent handler in the runtime demo.

Calls one tool, one plain internal function, and has a deliberate failure
path — a single run exercises every `kind` the trace schema defines
(workflow, agent, tool, function) plus the error phase.
"""

from __future__ import annotations

from apv_trace import trace

from .functions import normalize_text
from .tools import classify_ticket_tool, lookup_kb_tool


class TriageAgent:
    """Routes a support ticket to a canned resolution via keyword classification."""

    @trace(kind="agent")
    async def handle(self, ticket_text: str) -> str:
        if ticket_text.strip().upper() == "CRASH":
            # Deliberate failure path — proves the decorator's error phase
            # fires and the exception still propagates to the caller.
            raise RuntimeError("simulated triage failure")

        normalized = normalize_text(ticket_text)
        category = classify_ticket_tool(normalized)
        articles = lookup_kb_tool(category)
        if not articles:
            return f"No KB articles found for category {category!r}."
        return f"Suggested article: {articles[0]}"
