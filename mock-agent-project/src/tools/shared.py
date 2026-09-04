"""Shared helpers used by tools across multiple framework flavors.

The point of this module is the *fan-in* case for the L3 call graph: two or more
tools should end up with edges to the same helper node, and the visualizer needs
to render that without duplicating the helper.
"""

from __future__ import annotations


def normalize_query(query: str) -> str:
    """Trim, lower-case, and collapse whitespace in a search query.

    Called from both `langchain_tools.web_search_tool` and
    `custom_tools.internal_search_tool`. Do NOT inline it — the fan-in is the
    whole reason this file exists.
    """
    return " ".join(query.strip().lower().split())


def redact_pii(text: str) -> str:
    """Placeholder redactor. In a real system this would strip PII before logs."""
    return text  # mock: no-op


def build_result_envelope(
    source: str,
    payload: dict,
    *,
    trace_id: str | None = None,
) -> dict:
    """Wrap a tool result in the envelope the coordinator agent expects.

    Called from every custom tool. Keeps envelope shape in one place so the
    scanner sees a consistent target for the fan-in.
    """
    return {
        "source": source,
        "payload": payload,
        "trace_id": trace_id,
        "ok": True,
    }
