"""WebSocket message schema for `/ws/runtime` — a discriminated union on `type`.

Mirrored by hand in `packages/shared-types/runtime.ts`, matching how
`graph.ts` mirrors the domain models (see that file's own docstring: a
real generator is explicitly deferred, hand-rolled is "what shipping looks
like on day one"). These models use the same `alias_generator=to_camel`
convention as `domain.node`/`domain.edge` — unlike `trace.events.TraceEvent`,
these genuinely go straight to the TS frontend, so camelCase-on-the-wire is
the right call here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from agentic_visualizer.runtime.dead_letter import DeadLetterEntry
from agentic_visualizer.trace.events import TraceEvent

_RUNTIME_MSG_CONFIG = ConfigDict(
    frozen=True,
    alias_generator=to_camel,
    populate_by_name=True,
)

NodeStatus = Literal["idle", "active", "errored"]


class RuntimeErrorInfo(BaseModel):
    """Last-error detail for one node. Mirrors `apv_trace`'s `meta["error"]`
    shape (type/message/traceback) plus the event's own timestamp."""

    model_config = _RUNTIME_MSG_CONFIG

    type: str
    message: str
    traceback: str
    at: datetime

    @classmethod
    def from_event(cls, event: TraceEvent) -> RuntimeErrorInfo | None:
        """Build from a `phase="error"` event's `meta["error"]`. None if that's missing/malformed."""
        error = event.meta.get("error")
        if not isinstance(error, dict):
            return None
        return cls(
            type=str(error.get("type", "UnknownError")),
            message=str(error.get("message", "")),
            traceback=str(error.get("traceback", "")),
            at=event.ts,
        )


class NodeStateEntry(BaseModel):
    """One node's runtime state — the per-node shape shared by delta and snapshot."""

    model_config = _RUNTIME_MSG_CONFIG

    node_id: str
    status: NodeStatus
    run_count: int
    error_count: int
    last_error: RuntimeErrorInfo | None = None


class RuntimeDeltaMessage(BaseModel):
    """One node's state changed. Flat (not nested), matching Task 3's wire spec verbatim."""

    model_config = _RUNTIME_MSG_CONFIG

    type: Literal["delta"] = "delta"
    node_id: str
    status: NodeStatus
    run_count: int
    error_count: int
    last_error: RuntimeErrorInfo | None = None


class RuntimeSnapshotMessage(BaseModel):
    """Sent once, right after connect: every currently-tracked node's state."""

    model_config = _RUNTIME_MSG_CONFIG

    type: Literal["snapshot"] = "snapshot"
    states: list[NodeStateEntry]


class DeadLetterStatsMessage(BaseModel):
    """Sent periodically (every 2s, per Task 3's design). `count` is the dead-letter
    buffer's own size; `dropped_count`/`parse_error_count` come from the active
    TraceSource's stats — a different layer's concern (log parsing vs.
    resolution), combined here only because the wire message wants both."""

    model_config = _RUNTIME_MSG_CONFIG

    type: Literal["dead_letter_stats"] = "dead_letter_stats"
    count: int
    dropped_count: int
    parse_error_count: int


class RuntimeResetMessage(BaseModel):
    """Sent when the store resets — a fresh graph swapped in, all prior state is stale."""

    model_config = _RUNTIME_MSG_CONFIG

    type: Literal["reset"] = "reset"


RuntimeMessage = Annotated[
    RuntimeDeltaMessage | RuntimeSnapshotMessage | DeadLetterStatsMessage | RuntimeResetMessage,
    Field(discriminator="type"),
]


class DeadLetterEntryOut(BaseModel):
    """Wire shape for `GET /runtime/dead-letter` (added in Task 4 -- Task 3
    built `DeadLetterBuffer` but no REST endpoint for it yet; its own Files
    section flagged this as "add if missing"). Mirrors `dead_letter.DeadLetterEntry`
    field-for-field, camelCase on the wire like every other model here."""

    model_config = _RUNTIME_MSG_CONFIG

    kind: str
    ref_name: str
    ref_module: str
    ref_qualname: str | None
    phase: str
    reason: str
    recorded_at: datetime

    @classmethod
    def from_entry(cls, entry: DeadLetterEntry) -> DeadLetterEntryOut:
        return cls(
            kind=entry.kind,
            ref_name=entry.ref_name,
            ref_module=entry.ref_module,
            ref_qualname=entry.ref_qualname,
            phase=entry.phase,
            reason=entry.reason,
            recorded_at=entry.recorded_at,
        )
