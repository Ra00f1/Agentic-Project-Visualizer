"""The one registry of trace methods this app ships with.

To add a method:
  1. Write a `TraceSource` (implements `start`/`stop`/`events`/`status`).
  2. Write a `TraceMethod` with a flat settings model (`runtime/methods/base.py`).
  3. Add one line to `TRACE_METHODS` below.

The frontend picks it up automatically from `GET /runtime/methods` -- no
frontend change needed as long as the settings model stays flat
(`string`/`integer`/`boolean` fields only; see `tests/runtime/test_methods.py`'s
schema-subset test). If a future method needs a richer settings UI, add an
optional `ui_component: str | None` to `TraceMethodInfo` and a frontend
lookup table at that point -- not built now (Task 22's own Design: "Don't
build it now").

A plain dict, not a plugin loader: every method ships inside this app, so
there's nothing to discover at runtime -- one line per method is the
simplest registry that's still correct.
"""

from __future__ import annotations

from agentic_visualizer.runtime.methods.apv_live_file import ApvLiveFileMethod
from agentic_visualizer.runtime.methods.base import TraceMethod, TraceMethodInfo
from agentic_visualizer.runtime.methods.otlp_http import OtlpHttpMethod

TRACE_METHODS: dict[str, TraceMethod] = {m.id: m for m in (ApvLiveFileMethod(), OtlpHttpMethod())}

__all__ = ["TRACE_METHODS", "TraceMethod", "TraceMethodInfo"]
