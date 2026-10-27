"""Tool functions for the runtime demo.

`apv.name` on each span mirrors how a real target project's tools would be
named in its own catalog/DB. This fixture has no seeded DB (see
`../README.md`), so these names only get matched by the visualizer's
typed-name resolution strategy, not the higher-confidence db_id strategy —
see `packages/apv_trace/README.md`'s "db_id resolution via a manifest".
"""

from __future__ import annotations

from opentelemetry import trace

_tracer = trace.get_tracer(__name__)

# `apv.name` keeps the old decorator's `name="classify_ticket"` /
# `name="lookup_kb"` override; `apv.qualname` stays the real function name
# (`classify_ticket_tool` / `lookup_kb_tool`) — the two intentionally
# differ, mirroring a real target project where a catalog name and its
# implementing function's name don't match.
_CLASSIFY_TICKET_ATTRS = {
    "apv.kind": "tool",
    "apv.name": "classify_ticket",
    "apv.module": __name__,
    "apv.qualname": "classify_ticket_tool",
}

_LOOKUP_KB_ATTRS = {
    "apv.kind": "tool",
    "apv.name": "lookup_kb",
    "apv.module": __name__,
    "apv.qualname": "lookup_kb_tool",
}


def classify_ticket_tool(normalized_text: str) -> str:
    """Keyword-match a normalized ticket body to a coarse category."""
    with _tracer.start_as_current_span("classify_ticket_tool", attributes=_CLASSIFY_TICKET_ATTRS):
        if "refund" in normalized_text or "charge" in normalized_text:
            return "billing"
        if "error" in normalized_text or "exception" in normalized_text:
            return "technical"
        return "general"


def lookup_kb_tool(category: str) -> list[str]:
    """Return canned knowledge-base article titles for a category."""
    with _tracer.start_as_current_span("lookup_kb_tool", attributes=_LOOKUP_KB_ATTRS):
        catalog = {
            "billing": ["Understanding your invoice", "Requesting a refund"],
            "technical": ["Common error codes", "Reporting a bug"],
            "general": ["Getting started guide"],
        }
        return catalog.get(category, [])
