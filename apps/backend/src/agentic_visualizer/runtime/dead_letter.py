"""DeadLetterBuffer — the visible trail of trace events NodeIndex couldn't resolve.

Without this, an ambiguous or unmatched `ref` just looks like nothing
happened (per Task 3's own Risk note). Bounded so a target project stuck
emitting unresolvable events forever can't grow this without limit.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime

from agentic_visualizer.trace.events import TraceEvent

_DEFAULT_MAX_ENTRIES = 500


@dataclass(frozen=True, slots=True)
class DeadLetterEntry:
    """One event NodeIndex couldn't resolve, with enough of the original ref to debug it."""

    kind: str
    ref_name: str
    ref_module: str
    ref_qualname: str | None
    phase: str
    reason: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class DLStats:
    """Just this buffer's own count. See `RuntimeChannel` for how this is combined
    with the active `TraceSource`'s dropped/parse-error counts on the wire --
    those are a different layer's concern (log parsing, not resolution)."""

    count: int


class DeadLetterBuffer:
    """Bounded, thread-safe record of unresolved trace events.

    `threading.Lock`, not `asyncio.Lock` like everything else in `runtime/`
    -- deliberately inconsistent with the rest of the package, so worth
    explaining rather than leaving as an unexplained outlier. Every current
    caller (`RuntimeChannel._process_event`, `RuntimeChannel.dead_letter.stats()`
    from `_dead_letter_stats_loop`) is a plain asyncio coroutine on the one
    event loop, same as `RuntimeStateStore`; an `asyncio.Lock` (or even no
    lock at all, per `RuntimeStateStore.snapshot()`'s own reasoning) would
    be equally correct today. `threading.Lock` is used anyway because the
    task's own spec calls for "thread-safe" specifically, and this buffer's
    contents (a bounded debug trail) are the kind of thing a future
    diagnostics surface might reasonably want to read from a real worker
    thread without an event loop in scope. If that never materializes,
    switching to `asyncio.Lock` for consistency is a safe, easy follow-up.
    """

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._entries: deque[DeadLetterEntry] = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def record(self, event: TraceEvent, reason: str) -> None:
        entry = DeadLetterEntry(
            kind=event.kind,
            ref_name=event.ref.name,
            ref_module=event.ref.module,
            ref_qualname=event.ref.qualname,
            phase=event.phase,
            reason=reason,
            recorded_at=datetime.now(UTC),
        )
        with self._lock:
            self._entries.append(entry)

    def snapshot(self) -> list[DeadLetterEntry]:
        with self._lock:
            return list(self._entries)

    def stats(self) -> DLStats:
        with self._lock:
            return DLStats(count=len(self._entries))
