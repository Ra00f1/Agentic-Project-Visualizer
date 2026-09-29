"""Representative internal function(s) the tools/agent call.

Only a sample is instrumented, per the task scope — not every helper needs
a `@trace(kind="function")`, just enough to prove function-kind events and
span nesting work end to end.
"""

from __future__ import annotations

from apv_trace import trace


@trace(kind="function")
def normalize_text(text: str) -> str:
    """Lowercase and collapse whitespace before classification."""
    return " ".join(text.split()).lower()
