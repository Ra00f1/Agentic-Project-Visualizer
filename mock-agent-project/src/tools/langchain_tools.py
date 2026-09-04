"""LangChain-flavored tool definitions.

The `@tool` decorator is a stand-in for `langchain_core.tools.tool`. We import
it from a local shim so this mock project has no runtime dependency on
LangChain — but the decorator NAME and call shape match, which is what the
LangChain FrameworkAdapter will actually key off during scanning.
"""

from __future__ import annotations

from ..utils.decorators import tool  # LangChain-shaped decorator (local shim)
from .shared import normalize_query, build_result_envelope


@tool
def web_search_tool(query: str, max_results: int = 5) -> dict:
    """Search the public web for a query.

    Args:
        query: Free-text search string.
        max_results: Upper bound on results returned.

    The docstring matters: LangChain's real `@tool` decorator lifts the first
    paragraph into the tool description. Our adapter should do the same.
    """
    normalized = normalize_query(query)  # fan-in call — see shared.py
    return build_result_envelope(
        source="web_search",
        payload={"query": normalized, "results": [], "n": max_results},
    )


@tool
def summarize_url_tool(url: str) -> str:
    """Fetch a URL and return a short summary of its contents."""
    return f"summary-of:{url}"  # mock


@tool(return_direct=True)  # decorator called with an arg — different AST shape
def calculator_tool(expression: str) -> float:
    """Evaluate a simple arithmetic expression and return the numeric result.

    Note the `@tool(return_direct=True)` form: the scanner must handle both
    `@tool` (name reference) and `@tool(...)` (call node) as valid tool markers.
    """
    return 0.0  # mock
