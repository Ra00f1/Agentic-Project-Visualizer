"""Representative internal function(s) the tools/agent call.

Only a sample is instrumented, per the task scope — not every helper needs
a traced span, just enough to prove function-kind events and span nesting
work end to end.

Instrumented with the OpenTelemetry API directly (task 14), not the
`apv_trace.trace` decorator (task 13's `@trace(kind="function")` this file
used before) — proving a project that already speaks OTel needs only the
`apv.*` attribute convention from us, not our decorator. See
`../README.md`'s "How the demo is instrumented" for the convention; see
`apv_trace`'s own `configure()` (called once in `run_demo.py`) for how
these spans actually reach a JSONL file at all — plain OTel spans with no
provider attached go nowhere.
"""

from __future__ import annotations

from opentelemetry import trace

_tracer = trace.get_tracer(__name__)

# Static per-function attributes, built once at import time — unlike
# apv_trace's decorator (which can re-resolve `apv.name` per call via a
# `name` parameter), nothing here varies call to call, so there's no reason
# to rebuild this dict on every invocation. `apv.qualname` here is checked
# against the real `normalize_text.__qualname__` by
# `tests/test_demo_spans.py`'s drift-protection test — see that test's own
# docstring for why a hand-written qualname needs a guard at all.
_NORMALIZE_TEXT_ATTRS = {
    "apv.kind": "function",
    "apv.name": "normalize_text",
    "apv.module": __name__,
    "apv.qualname": "normalize_text",
}


def normalize_text(text: str) -> str:
    """Lowercase and collapse whitespace before classification."""
    with _tracer.start_as_current_span("normalize_text", attributes=_NORMALIZE_TEXT_ATTRS):
        return " ".join(text.split()).lower()
