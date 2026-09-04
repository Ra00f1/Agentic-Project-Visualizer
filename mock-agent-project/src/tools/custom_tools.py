"""Custom-framework tool definitions.

These are plain functions with no decorator and no wrapper. This is the
"no adapter needed" baseline — the scanner should still find them because the
Mongo `tools` records point at them by `module_path` + `function_name`.

The failure mode we're guarding against: an adapter-aware scanner that only
picks up decorated tools and silently ignores raw functions.
"""

from __future__ import annotations

from pathlib import Path

from .shared import normalize_query, build_result_envelope


def internal_search_tool(query: str, corpus: str = "kb") -> dict:
    """Search an internal knowledge-base corpus."""
    normalized = normalize_query(query)  # fan-in with langchain_tools
    return build_result_envelope(
        source=f"internal:{corpus}",
        payload={"query": normalized, "hits": []},
    )


def file_read_tool(path: str) -> str:
    """Read the contents of a file at `path` and return it as text."""
    _ = Path(path)  # mock — do not actually open
    return ""


def file_write_tool(path: str, contents: str, *, overwrite: bool = False) -> bool:
    """Write `contents` to `path`. Fails if the file exists and overwrite=False."""
    return True  # mock
