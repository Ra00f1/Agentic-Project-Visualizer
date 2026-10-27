"""Runtime-tracing consumption layer: TraceSource -> correlation -> state -> WebSocket.

See `.claude/CLAUDE.md` Section 1 "Runtime overlay" -- everything in this package
only ever reads a log file a target project already wrote to. Nothing here
imports, executes, or introspects the target project itself.
"""

from __future__ import annotations

from agentic_visualizer.runtime.channel import MethodConfig, RuntimeChannel
from agentic_visualizer.runtime.dead_letter import DeadLetterBuffer, DeadLetterEntry, DLStats
from agentic_visualizer.runtime.messages import (
    DeadLetterEntryOut,
    DeadLetterStatsMessage,
    NodeStateEntry,
    NodeStatus,
    RuntimeDeltaMessage,
    RuntimeErrorInfo,
    RuntimeMessage,
    RuntimeResetMessage,
    RuntimeSnapshotMessage,
    SourceStatus,
)
from agentic_visualizer.runtime.methods import TRACE_METHODS, TraceMethod, TraceMethodInfo
from agentic_visualizer.runtime.node_index import NodeIndex, Resolved, ResolveResult, Unresolved
from agentic_visualizer.runtime.sources.base import TraceSource
from agentic_visualizer.runtime.sources.file_tail import FileTailStats, FileTailTraceSource
from agentic_visualizer.runtime.state import (
    EvictedSpan,
    NodeDelta,
    NodeRuntimeState,
    RuntimeStateStore,
)

__all__ = [
    "DLStats",
    "DeadLetterBuffer",
    "DeadLetterEntry",
    "DeadLetterEntryOut",
    "DeadLetterStatsMessage",
    "EvictedSpan",
    "FileTailStats",
    "FileTailTraceSource",
    "MethodConfig",
    "NodeDelta",
    "NodeIndex",
    "NodeRuntimeState",
    "NodeStateEntry",
    "NodeStatus",
    "Resolved",
    "ResolveResult",
    "RuntimeChannel",
    "RuntimeDeltaMessage",
    "RuntimeErrorInfo",
    "RuntimeMessage",
    "RuntimeResetMessage",
    "RuntimeSnapshotMessage",
    "RuntimeStateStore",
    "SourceStatus",
    "TRACE_METHODS",
    "TraceMethod",
    "TraceMethodInfo",
    "TraceSource",
    "Unresolved",
]
