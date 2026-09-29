"""Runtime-tracing consumption layer: TraceSource -> correlation -> state -> WebSocket.

See `.claude/CLAUDE.md` Section 1 "Runtime overlay" -- everything in this package
only ever reads a log file a target project already wrote to. Nothing here
imports, executes, or introspects the target project itself.
"""

from __future__ import annotations

from agentic_visualizer.runtime.channel import RuntimeChannel
from agentic_visualizer.runtime.dead_letter import DeadLetterBuffer, DeadLetterEntry, DLStats
from agentic_visualizer.runtime.manager import RuntimeManager
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
)
from agentic_visualizer.runtime.node_index import NodeIndex, Resolved, ResolveResult, Unresolved
from agentic_visualizer.runtime.sources.base import TraceSource
from agentic_visualizer.runtime.sources.file_tail import FileTailStats, FileTailTraceSource
from agentic_visualizer.runtime.state import NodeDelta, NodeRuntimeState, RuntimeStateStore

__all__ = [
    "DLStats",
    "DeadLetterBuffer",
    "DeadLetterEntry",
    "DeadLetterEntryOut",
    "DeadLetterStatsMessage",
    "FileTailStats",
    "FileTailTraceSource",
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
    "RuntimeManager",
    "RuntimeMessage",
    "RuntimeResetMessage",
    "RuntimeSnapshotMessage",
    "RuntimeStateStore",
    "TraceSource",
    "Unresolved",
]
