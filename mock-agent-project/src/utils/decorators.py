"""Decorator helpers.

We include:
  1. `tool` — a LangChain-shaped decorator that supports both `@tool` and
     `@tool(return_direct=True)` forms. The two forms produce different AST
     nodes (Name vs Call), and the scanner's LangChain adapter must handle both.
  2. `timed` — a `functools.wraps`-based wrapper. The scanner should surface
     the WRAPPED function's name in the graph, not the wrapper's.
  3. `looks_like_a_tool_but_isnt` — a function with a tool-shaped signature and
     docstring but NO decorator and NO Mongo record. It must NOT appear as a
     tool node. If it does, the adapter is over-triggering.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def tool(fn_or_kwarg: Any = None, **_options: Any) -> Any:
    """LangChain-shaped `@tool` shim.

    Usage:
        @tool
        def foo(...): ...

        @tool(return_direct=True)
        def bar(...): ...
    """
    if callable(fn_or_kwarg):
        # Bare `@tool` form.
        fn = fn_or_kwarg
        fn.__is_tool__ = True  # type: ignore[attr-defined]
        return fn

    # `@tool(...)` form — return a decorator.
    def _decorator(fn: F) -> F:
        fn.__is_tool__ = True  # type: ignore[attr-defined]
        return fn

    return _decorator


def timed(fn: F) -> F:
    """Wrap `fn` with a fake timing hook, preserving name/docstring via wraps.

    The scanner should attribute calls made from the wrapped function to `fn`,
    not to the inner `wrapper`. functools.wraps preserves __wrapped__ which is
    the seam we'll follow.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        # mock: no actual timing
        return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def looks_like_a_tool_but_isnt(query: str, max_results: int = 5) -> dict:
    """Search-style signature and docstring but not registered as a tool.

    This function is a *distractor*. It has no `@tool` decorator, and no Mongo
    tool row references it. If the visualizer surfaces it as a tool node, the
    LangChain adapter is doing signature-based inference where it shouldn't.
    """
    return {"query": query, "results": [], "n": max_results}
