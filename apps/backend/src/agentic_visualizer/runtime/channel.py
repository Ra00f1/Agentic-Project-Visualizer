"""RuntimeChannel — wires TraceSource -> NodeIndex.resolve -> RuntimeStateStore.apply
-> batching -> WebSocket publish.

**Deviation from Task 3's `## Design`, flagged up front:** the task describes
a `RefreshCompleted` event this channel subscribes to, with `Graph` living as
persistent, refreshable server-side state. Neither exists in this codebase —
`main.py`'s own docstring states the policy plainly: "No caching, no
persistence — every call is a fresh scan," and `domain/graph.py`'s `Graph`
is a stateless, immutable value type with no event bus. `update_graph()`
below is that same swap-under-lock mechanism from the task's design, just
triggered by an explicit call instead of a subscription — wired so
`/graph`'s request handler calls it after every successful scan, since a
`/graph` call *is* what "refresh" means in this app today. See
`.claude/Task 3.md`'s `## Result` for the full reasoning.

Three background tasks, started together: `_consume_loop` (drains the active
TraceSource, resolves, applies, queues deltas for batching), `_batch_flush_loop`
(every `batch_window_seconds`, ships the coalesced deltas), and
`_dead_letter_stats_loop` (every `dead_letter_stats_interval_seconds`, ships
buffer + parse/drop counts).

**One lock, not several.** `update_graph()` (index swap + state reset +
pending clear) and `_consume_loop`'s per-event work (resolve + apply +
pending update) both hold `self._lock` for their *entire* operation, not
separate locks per structure. Earlier drafts used one lock per structure
(`_index_lock`, `_pending_lock`, plus `RuntimeStateStore`'s own internal
lock) — that protects each structure individually but not the sequence
across them: an event could resolve against a just-swapped index, get
applied, and land a delta in `_pending` in the exact window between
`update_graph()`'s `state_store.reset()` and its `_pending.clear()` —
which then wipes that delta before any client ever saw it, while the state
store itself already reflects the change. One lock covering "one full
graph refresh" and "one full event's processing" as indivisible units
closes that window.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from agentic_visualizer.domain import Graph
from agentic_visualizer.runtime.dead_letter import DeadLetterBuffer
from agentic_visualizer.runtime.manager import RuntimeManager
from agentic_visualizer.runtime.messages import (
    DeadLetterStatsMessage,
    NodeStateEntry,
    RuntimeDeltaMessage,
    RuntimeMessage,
    RuntimeResetMessage,
    RuntimeSnapshotMessage,
)
from agentic_visualizer.runtime.node_index import NodeIndex, Unresolved
from agentic_visualizer.runtime.sources.base import TraceSource
from agentic_visualizer.runtime.sources.file_tail import FileTailTraceSource
from agentic_visualizer.runtime.state import NodeDelta, RuntimeStateStore
from agentic_visualizer.trace.events import TraceEvent

logger = logging.getLogger("agentic_visualizer")

_DEFAULT_BATCH_WINDOW_SECONDS = 0.05
_DEFAULT_DEAD_LETTER_STATS_INTERVAL_SECONDS = 2.0
_NO_SOURCE_POLL_SECONDS = 5.0
"""Fallback poll interval for _consume_loop when there's no active source.
Just a safety net: set_source() wakes the loop immediately via
`_source_ready`, so this only matters if that signal is ever missed."""


class RuntimeChannel:
    """Owns the live NodeIndex, RuntimeStateStore, DeadLetterBuffer, and WS fan-out."""

    def __init__(
        self,
        *,
        runtime_manager: RuntimeManager | None = None,
        batch_window_seconds: float = _DEFAULT_BATCH_WINDOW_SECONDS,
        dead_letter_stats_interval_seconds: float = _DEFAULT_DEAD_LETTER_STATS_INTERVAL_SECONDS,
        max_dead_letter_entries: int = 500,
    ) -> None:
        self._runtime_manager = runtime_manager or RuntimeManager()
        self._current_source: TraceSource | None = None
        self._source_ready = asyncio.Event()

        self._index: NodeIndex = NodeIndex.build(Graph())
        self.state_store = RuntimeStateStore()
        self.dead_letter = DeadLetterBuffer(max_entries=max_dead_letter_entries)

        # Guards self._index and self._pending together -- see module
        # docstring on why one lock, not one per structure.
        self._lock = asyncio.Lock()
        self._pending: dict[str, NodeDelta] = {}

        self._batch_window_seconds = batch_window_seconds
        self._dead_letter_stats_interval_seconds = dead_letter_stats_interval_seconds

        self._subscribers: set[asyncio.Queue[RuntimeMessage]] = set()
        self._tasks: list[asyncio.Task[None]] = []

    # --- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        if self._tasks:
            raise RuntimeError("RuntimeChannel is already started")
        self._tasks = [
            asyncio.create_task(self._consume_loop(), name="runtime_consume_loop"),
            asyncio.create_task(self._batch_flush_loop(), name="runtime_batch_flush_loop"),
            asyncio.create_task(self._dead_letter_stats_loop(), name="runtime_dead_letter_stats_loop"),
        ]

    async def stop(self) -> None:
        """Idempotent: calling this before `start()` or more than once is a no-op.

        Every task's await is guarded individually (matching
        `FileTailTraceSource.stop()`'s pattern) so one loop dying from a bug
        doesn't stop the others from being awaited, and doesn't leave this
        method re-raising forever on every future call -- `main.py`'s
        `_lifespan` depends on this returning cleanly to reach
        `close_all_clients()` on shutdown.
        """
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a background loop's own bug must not wedge shutdown
                logger.exception("runtime: %s ended with an error during stop()", task.get_name())
        await self._runtime_manager.stop()

    async def set_source(self, source: TraceSource) -> None:
        """Point the channel at a new TraceSource -- e.g. a FileTailTraceSource
        tailing a target project's trace log. Stops/replaces any active one."""
        await self._runtime_manager.start(source)
        self._current_source = source
        self._source_ready.set()

    # --- graph refresh --------------------------------------------------

    async def update_graph(self, graph: Graph) -> None:
        """Rebuild the NodeIndex from a fresh Graph, reset runtime state, publish a reset.

        Events resolved between "new graph available" and "swap under lock"
        go against the old index -- that's fine, they were emitted against
        the world the old index describes (see Task 3's own Design note).
        """
        new_index = NodeIndex.build(graph)
        async with self._lock:
            self._index = new_index
            await self.state_store.reset()
            self._pending.clear()
        self._publish_nowait(RuntimeResetMessage())

    # --- WebSocket fan-out ----------------------------------------------

    def subscribe(self) -> asyncio.Queue[RuntimeMessage]:
        """Register a new subscriber. Caller MUST call `unsubscribe` when the connection closes."""
        queue: asyncio.Queue[RuntimeMessage] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[RuntimeMessage]) -> None:
        self._subscribers.discard(queue)

    def snapshot_message(self) -> RuntimeSnapshotMessage:
        """The current full runtime state, for a client that just connected."""
        states = [
            NodeStateEntry(
                node_id=d.node_id,
                status=d.status,
                run_count=d.run_count,
                error_count=d.error_count,
                last_error=d.last_error,
            )
            for d in self.state_store.snapshot()
        ]
        return RuntimeSnapshotMessage(states=states)

    def _publish_nowait(self, message: RuntimeMessage) -> None:
        # Subscriber queues are unbounded: this app has at most a handful of
        # WS clients (a single desktop user) and low message volume (batched
        # deltas + one stats message per 2s + rare resets) -- no realistic
        # scenario where a queue grows large enough to matter.
        for queue in list(self._subscribers):
            queue.put_nowait(message)

    # --- background loops -------------------------------------------------

    async def _consume_loop(self) -> None:
        while True:
            try:
                events_iter = self._runtime_manager.events()
            except RuntimeError:
                # No active source yet (or it was just stopped/replaced).
                # Not an error -- the runtime overlay is opt-in.
                await self._wait_for_next_source()
                continue

            try:
                async for event in events_iter:
                    await self._process_event(event)
            except Exception:  # noqa: BLE001 - the event stream itself must not kill this task
                logger.exception("runtime: consume loop's event stream ended unexpectedly")
            # events_iter ended -- the source was stopped/replaced, or the
            # stream itself failed above. Loop back rather than exiting.
            await self._wait_for_next_source()

    async def _process_event(self, event: TraceEvent) -> None:
        """Resolve + apply one event. Never lets a bug here kill `_consume_loop`."""
        try:
            async with self._lock:
                result = self._index.resolve(event)
                if isinstance(result, Unresolved):
                    self.dead_letter.record(event, result.reason)
                    logger.info(
                        "runtime: unresolved event kind=%s ref=%r reason=%s",
                        event.kind,
                        event.ref,
                        result.reason,
                    )
                    return
                deltas = await self.state_store.apply(event, result)
                for delta in deltas:
                    self._pending[delta.node_id] = delta
        except Exception:  # noqa: BLE001 - one bad event must not stop the whole stream
            logger.exception(
                "runtime: failed to process trace event kind=%s ref=%r", event.kind, event.ref
            )

    async def _wait_for_next_source(self) -> None:
        """Block until `set_source()` signals a new source, or the poll timeout, whichever first."""
        self._source_ready.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._source_ready.wait(), timeout=_NO_SOURCE_POLL_SECONDS)

    async def _batch_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._batch_window_seconds)
            try:
                async with self._lock:
                    if not self._pending:
                        continue
                    deltas = list(self._pending.values())
                    self._pending.clear()
                for delta in deltas:
                    self._publish_nowait(
                        RuntimeDeltaMessage(
                            node_id=delta.node_id,
                            status=delta.status,
                            run_count=delta.run_count,
                            error_count=delta.error_count,
                            last_error=delta.last_error,
                        )
                    )
            except Exception:  # noqa: BLE001 - a bad flush must not kill future flushes
                logger.exception("runtime: batch flush failed")

    async def _dead_letter_stats_loop(self) -> None:
        while True:
            await asyncio.sleep(self._dead_letter_stats_interval_seconds)
            try:
                dropped_count, parse_error_count = self._source_parse_stats()
                self._publish_nowait(
                    DeadLetterStatsMessage(
                        count=self.dead_letter.stats().count,
                        dropped_count=dropped_count,
                        parse_error_count=parse_error_count,
                    )
                )
            except Exception:  # noqa: BLE001 - a bad stats publish must not kill future ones
                logger.exception("runtime: dead-letter stats publish failed")

    def _source_parse_stats(self) -> tuple[int, int]:
        """(dropped_count, parse_error_count) from the active source, if it's a FileTailTraceSource.

        Not part of the `TraceSource` Protocol (Task 2) -- specific to this
        one implementation. `isinstance` (not duck-typing) so a rename of
        `FileTailStats`'s fields fails mypy here instead of silently
        reporting zeros; a future TraceSource without these counters just
        isn't a FileTailTraceSource and reports 0s the same way.
        """
        if not isinstance(self._current_source, FileTailTraceSource):
            return (0, 0)
        stats = self._current_source.stats()
        return (stats.dropped_count, stats.parse_error_count)
