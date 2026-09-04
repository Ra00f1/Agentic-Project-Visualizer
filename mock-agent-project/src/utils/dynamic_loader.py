"""Intentional dynamic-import fixture.

KNOWN LIMITATION: the v1 AST-based scanner cannot statically resolve
`importlib.import_module` targets when the argument is a variable. Rather than
try to solve that in v1, we surface it as an *unresolved reference* on the
graph, with the source line as provenance. This module gives us something to
point at.

Do NOT rewrite this to use static imports — it's a fixture, not a bug.
"""

from __future__ import annotations

import importlib
from typing import Any


def load_plugin(module_name: str, symbol: str) -> Any:
    """Dynamically import a plugin module and return one of its symbols.

    Example: `load_plugin("src.tools.custom_tools", "internal_search_tool")`.
    The scanner should record this call site as `unresolved` and move on.
    """
    module = importlib.import_module(module_name)
    return getattr(module, symbol)
