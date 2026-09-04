"""Output-shaping helpers exposed as agent tools.

Placed at `src/skills/` — deliberately NOT under `src/tools/` — to
exercise the L2 scanner's name-based fallback. If L2 assumed tool
implementations always live in a folder named `tools`, the tool
`moved_tool` (whose Mongo `import_path` is stale) would never resolve.
"""

from __future__ import annotations


def format_output_tool(items: list[dict], *, style: str = "table") -> str:
    """Format a list of dict results into a human-readable payload.

    Style choices: "table", "bullets", "json". The wrapping agent picks
    one based on the requesting user's preferences.
    """
    if style == "json":
        import json
        return json.dumps(items, indent=2)
    if style == "bullets":
        return "\n".join(f"- {row!r}" for row in items)
    # Default: rudimentary table.
    if not items:
        return "(no rows)"
    headers = list(items[0].keys())
    lines = [" | ".join(headers)]
    for row in items:
        lines.append(" | ".join(str(row.get(h, "")) for h in headers))
    return "\n".join(lines)
