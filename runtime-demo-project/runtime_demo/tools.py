"""Tool functions for the runtime demo.

`name=` on each `@trace` mirrors how a real target project's tools would be
named in its own catalog/DB. There's no seeded DB behind this fixture (see
../README.md), so these names only get matched by the visualizer's
typed-name resolution strategy, not the db_id strategy.
"""

from __future__ import annotations

from apv_trace import trace


@trace(kind="tool", name="classify_ticket")
def classify_ticket_tool(normalized_text: str) -> str:
    """Keyword-match a normalized ticket body to a coarse category."""
    if "refund" in normalized_text or "charge" in normalized_text:
        return "billing"
    if "error" in normalized_text or "exception" in normalized_text:
        return "technical"
    return "general"


@trace(kind="tool", name="lookup_kb")
def lookup_kb_tool(category: str) -> list[str]:
    """Return canned knowledge-base article titles for a category."""
    catalog = {
        "billing": ["Understanding your invoice", "Requesting a refund"],
        "technical": ["Common error codes", "Reporting a bug"],
        "general": ["Getting started guide"],
    }
    return catalog.get(category, [])
