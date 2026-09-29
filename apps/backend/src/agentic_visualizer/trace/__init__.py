"""Runtime-trace wire schema for the Agentic Project Visualizer.

This package defines ONLY the contract a target project's trace log must
satisfy — the shape a `TraceSource` (task NN+1) parses lines into. It does
not read, tail, or watch any file itself, and it is never imported by a
target project. See `.claude/CLAUDE.md` §1 "Runtime overlay" for the
read-only boundary this app keeps around runtime tracing.
"""

from __future__ import annotations

from agentic_visualizer.trace.events import TraceError, TraceEvent, TraceRef

__all__ = ["TraceError", "TraceEvent", "TraceRef"]
