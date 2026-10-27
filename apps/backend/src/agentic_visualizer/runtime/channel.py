"""RuntimeChannel — wires N TraceSources -> NodeIndex.resolve -> RuntimeStateStore.apply
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

**Task 22: several sources at once, `RuntimeManager` folded in.** Task 2's
`RuntimeManager` owned at most one active `TraceSource`, fed through one
shared `_consume_loop`. The Setup screen can now turn on more than one
tracing method at a time (Task 22's whole point), so each configured
source gets its OWN consume task (`_consume_source_loop`) instead of one
loop juggling a single active source — simpler than the old "wait for the
next source" polling dance, and it gives per-source crash isolation for
free: one source's task failing doesn't touch any other's. `self._sources:
dict[str, _RunningSource]`, keyed by `TraceMethod.id`, is the whole of
what used to be `RuntimeManager`'s job.

Three background tasks, started together: `_batch_flush_loop` (every
`batch_window_seconds`, ships the coalesced deltas), `_dead_letter_stats_loop`
(every `dead_letter_stats_interval_seconds`, ships buffer + per-source
status), and `_orphan_sweep_loop`. Per-source consume tasks are tracked
separately, in `self._sources`, not in `self._tasks` — their lifetime is
tied to `apply_config`, not to `start()`/`stop()`.

**One lock, not several.** `update_graph()` (index swap + state reset +
pending clear), `apply_config()` (the whole start/stop/restart diff), and
`_process_event()`'s per-event work all hold `self._lock` for their *entire*
operation, not separate locks per structure. Earlier drafts used one lock
per structure (`_index_lock`, `_pending_lock`, plus `RuntimeStateStore`'s
own internal lock) — that protects each structure individually but not the
sequence across them: an event could resolve against a just-swapped index,
get applied, and land a delta in `_pending` in the exact window between
`update_graph()`'s `state_store.reset()` and its `_pending.clear()` —
which then wipes that delta before any client ever saw it, while the state
store itself already reflects the change. One lock covering each of these
as an indivisible unit closes that window. `apply_config()` calling its own
private, ALREADY-locked helpers (`_start_source_locked`/`_stop_source_locked`/
`_source_statuses_locked`) rather than the public lock-taking versions is
what keeps this from deadlocking on `asyncio.Lock`'s non-reentrancy.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel

from agentic_visualizer.domain import Graph
from agentic_visualizer.runtime.dead_letter import DeadLetterBuffer
from agentic_visualizer.runtime.messages import (
    DeadLetterStatsMessage,
    NodeStateEntry,
    RuntimeDeltaMessage,
    RuntimeMessage,
    RuntimeResetMessage,
    RuntimeSnapshotMessage,
    SourceStatus,
)
from agentic_visualizer.runtime.methods import TRACE_METHODS
from agentic_visualizer.runtime.methods.base import TraceMethod
from agentic_visualizer.runtime.node_index import NodeIndex, Unresolved
from agentic_visualizer.runtime.sources.base import TraceSource
from agentic_visualizer.runtime.sources.file_tail import FileTailTraceSource
from agentic_visualizer.runtime.sources.otlp_http import OtlpHttpTraceSource
from agentic_visualizer.runtime.state import NodeDelta, RuntimeStateStore
from agentic_visualizer.trace.events import TraceEvent

logger = logging.getLogger("agentic_visualizer")

_DEFAULT_BATCH_WINDOW_SECONDS = 0.05
_DEFAULT_DEAD_LETTER_STATS_INTERVAL_SECONDS = 2.0
_DEFAULT_ORPHANED_SPAN_TTL_SECONDS = 300.0
"""5 minutes -- a guess (Task 9's own Risk note), conservative enough that a
genuinely slow tool call is unlikely to get evicted mid-run. Override via
`AGENTIC_ORPHANED_SPAN_TTL_SECONDS` if a real target project needs longer."""
_ORPHAN_SWEEP_INTERVAL_SECONDS = 30.0
_DEDUP_CACHE_SIZE = 20_000
"""Task 23: if a target runs BOTH `apv_trace` (file-tail) and exports OTLP
to this app at once, the same span's `start`/`end`/`error` arrives twice --
once live from the file, once completed from OTLP. `apv_trace` writes
OTel's own span id in hex (Task 13), so `(span_id, phase)` identifies "the
same lifecycle boundary of the same span" regardless of which source it
came from. 20,000 is generous for any realistic single run without growing
unbounded -- oldest entries are evicted once the cap is hit."""


@dataclass(frozen=True, slots=True)
class MethodConfig:
    """One requested trace-method entry for `apply_config` — the method id
    plus its ALREADY-VALIDATED settings. The caller (the `PUT
    /runtime/config` handler, or `_lifespan`'s env-var startup default)
    validates raw JSON against that method's own `settings_model` first;
    `apply_config` never does JSON validation itself."""

    id: str
    settings: BaseModel


@dataclass
class _RunningSource:
    """One entry in `RuntimeChannel._sources`. Either `source`/`task` are
    both set (the method started successfully — `error` is None), or
    `error` is set and both are None (the method failed to construct or
    start — see `_start_source_locked`). Never a mix of the two: that
    invariant is what lets `_source_statuses_locked` branch on `error is
    None` alone instead of separately checking `source`/`task`."""

    method_id: str
    settings: BaseModel
    source: TraceSource | None
    task: asyncio.Task[None] | None
    error: str | None
    events_received: int = field(default=0)
    events_ignored: int = field(default=0)


class RuntimeChannel:
    """Owns the live NodeIndex, RuntimeStateStore, DeadLetterBuffer, the
    running trace sources, and the /ws/runtime fan-out."""

    def __init__(
        self,
        *,
        methods: Mapping[str, TraceMethod] | None = None,
        batch_window_seconds: float = _DEFAULT_BATCH_WINDOW_SECONDS,
        dead_letter_stats_interval_seconds: float = _DEFAULT_DEAD_LETTER_STATS_INTERVAL_SECONDS,
        max_dead_letter_entries: int = 500,
        orphaned_span_ttl_seconds: float = _DEFAULT_ORPHANED_SPAN_TTL_SECONDS,
        orphan_sweep_interval_seconds: float = _ORPHAN_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        # Injectable so tests can register a fake method without touching
        # the real, app-wide TRACE_METHODS registry (same pattern Task 2's
        # `runtime_manager` param used to follow for a fake TraceSource).
        self._methods: Mapping[str, TraceMethod] = methods if methods is not None else TRACE_METHODS
        self._sources: dict[str, _RunningSource] = {}

        self._index: NodeIndex = NodeIndex.build(Graph())
        self.state_store = RuntimeStateStore()
        self.dead_letter = DeadLetterBuffer(max_entries=max_dead_letter_entries)

        # Guards self._index, self._pending, AND self._sources together --
        # see module docstring on why one lock, not one per structure.
        self._lock = asyncio.Lock()
        self._pending: dict[str, NodeDelta] = {}
        # Insertion-ordered so the cap eviction below can always evict the
        # OLDEST entry in O(1) -- same rationale as `_JsonlSpanProcessor._live`
        # in apv_trace. Guarded by `self._lock` like everything else
        # `_process_event` touches.
        self._seen_span_phases: OrderedDict[tuple[str, str], None] = OrderedDict()

        self._batch_window_seconds = batch_window_seconds
        self._dead_letter_stats_interval_seconds = dead_letter_stats_interval_seconds
        self._orphaned_span_ttl_seconds = orphaned_span_ttl_seconds
        self._orphan_sweep_interval_seconds = orphan_sweep_interval_seconds

        self._subscribers: set[asyncio.Queue[RuntimeMessage]] = set()
        self._tasks: list[asyncio.Task[None]] = []

        # Cumulative for the channel's lifetime (not reset by update_graph
        # -- describes the trace STREAM, not the graph). Kept as a single
        # total per Task 22's own Design ("keep events_resolved ... as
        # totals"); per-source received/ignored counts live on each
        # `_RunningSource` instead.
        self._events_resolved = 0

    # --- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        if self._tasks:
            raise RuntimeError("RuntimeChannel is already started")
        self._tasks = [
            asyncio.create_task(self._batch_flush_loop(), name="runtime_batch_flush_loop"),
            asyncio.create_task(self._dead_letter_stats_loop(), name="runtime_dead_letter_stats_loop"),
            asyncio.create_task(self._orphan_sweep_loop(), name="runtime_orphan_sweep_loop"),
        ]

    async def stop(self) -> None:
        """Idempotent: calling this before `start()` or more than once is a no-op.

        Every background task's await is guarded individually so one loop
        dying from a bug doesn't stop the others from being awaited, and
        doesn't leave this method re-raising forever on every future call
        -- `main.py`'s `_lifespan` depends on this returning cleanly to
        reach `close_all_clients()` on shutdown. Every running trace
        source is stopped too, same as the background tasks.
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
        async with self._lock:
            for method_id in list(self._sources):
                await self._stop_source_locked(method_id)

    # --- trace method configuration (Task 22) ----------------------------

    async def apply_config(self, entries: list[MethodConfig]) -> list[SourceStatus]:
        """Reconcile the running sources against `entries`: stop any
        running source whose id isn't in `entries`, start any requested id
        that isn't running yet, and restart one whose settings changed.
        Unchanged entries (same id, equal settings) are left running
        as-is. A source that fails to start doesn't stop the others —
        each gets its own try/except in `_start_source_locked`.

        Holds the lock for the whole diff, matching the module docstring's
        "one lock covers one full operation."
        """
        async with self._lock:
            requested_ids = {e.id for e in entries}
            for method_id in [m for m in self._sources if m not in requested_ids]:
                await self._stop_source_locked(method_id)

            for entry in entries:
                existing = self._sources.get(entry.id)
                if existing is not None and existing.error is None and existing.settings == entry.settings:
                    continue  # unchanged -- leave it running
                if existing is not None:
                    await self._stop_source_locked(entry.id)
                await self._start_source_locked(entry.id, entry.settings)

            return self._source_statuses_locked()

    async def source_statuses(self) -> list[SourceStatus]:
        """Current status of every configured source, in `self._sources`'
        (insertion) order. Safe to call at any time."""
        async with self._lock:
            return self._source_statuses_locked()

    async def active_methods(self) -> list[MethodConfig]:
        """The currently-running `(id, settings)` pairs, for `GET
        /runtime/config`'s `methods` echo."""
        async with self._lock:
            return [MethodConfig(id=r.method_id, settings=r.settings) for r in self._sources.values()]

    def _source_statuses_locked(self) -> list[SourceStatus]:
        """Caller must hold `self._lock`. Synchronous and side-effect-free
        (`TraceSource.status()` must not block, per its own Protocol
        contract), so this never awaits -- keeping it a plain function
        (not `async`) is what lets `apply_config` call it without
        re-entering `self._lock`."""
        statuses: list[SourceStatus] = []
        for running in self._sources.values():
            label = self._label_for(running.method_id)
            if running.error is not None:
                statuses.append(
                    SourceStatus(
                        method_id=running.method_id,
                        label=label,
                        state="error",
                        detail=running.error,
                        events_received=running.events_received,
                        events_ignored=running.events_ignored,
                    )
                )
                continue
            assert running.source is not None  # invariant: error is None => source is set
            own_status = running.source.status()
            # `OtlpHttpTraceSource` tracks its OWN `events_ignored` --
            # spans filtered by `span_mapping.classify` before ever
            # reaching `_process_event`, so they never bump the
            # channel-tracked counter below. Summed here (not instead of
            # it) so this field reflects BOTH "never made it past
            # classification" and "reached NodeIndex but didn't resolve" --
            # see `OtlpHttpTraceSource.events_ignored`'s own docstring.
            events_ignored = running.events_ignored
            if isinstance(running.source, OtlpHttpTraceSource):
                events_ignored += running.source.events_ignored
            statuses.append(
                own_status.model_copy(
                    update={
                        "method_id": running.method_id,
                        "label": label,
                        "events_received": running.events_received,
                        "events_ignored": events_ignored,
                    }
                )
            )
        return statuses

    def _label_for(self, method_id: str) -> str:
        method = self._methods.get(method_id)
        # Falls back to the id itself -- reachable if a source is still
        # running under an id that's since been removed from the registry
        # (not possible with today's fixed, app-shipped method list, but
        # cheap insurance against a KeyError over a cosmetic label).
        return method.info().label if method is not None else method_id

    async def _start_source_locked(self, method_id: str, settings: BaseModel) -> None:
        """Caller must hold `self._lock`."""
        method = self._methods.get(method_id)
        if method is None:
            # The PUT handler already rejects unknown ids with a 422
            # before this is ever reached -- this branch exists only to
            # keep `apply_config` safe to call with a manually-built
            # `MethodConfig` (as `_lifespan`'s startup default does).
            logger.error("runtime: apply_config requested unknown method id %r", method_id)
            self._sources[method_id] = _RunningSource(
                method_id=method_id,
                settings=settings,
                source=None,
                task=None,
                error=f"unknown trace method id: {method_id!r}",
            )
            return
        try:
            source = method.create_source(settings)
            await source.start()
        except Exception as exc:  # noqa: BLE001 - one bad source must not block the others from starting
            logger.exception("runtime: failed to start trace method %s", method_id)
            self._sources[method_id] = _RunningSource(
                method_id=method_id, settings=settings, source=None, task=None, error=str(exc)
            )
            return
        task = asyncio.create_task(
            self._consume_source_loop(method_id, source), name=f"runtime_consume_{method_id}"
        )
        self._sources[method_id] = _RunningSource(
            method_id=method_id, settings=settings, source=source, task=task, error=None
        )

    async def _stop_source_locked(self, method_id: str) -> None:
        """Caller must hold `self._lock`. Idempotent: popping a missing id is a no-op."""
        running = self._sources.pop(method_id, None)
        if running is None:
            return
        if running.source is not None:
            await running.source.stop()
        if running.task is not None:
            running.task.cancel()
            try:
                await running.task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a dying consume task must not wedge stop
                logger.exception("runtime: consume task for %s ended with an error during stop", method_id)

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
                last_ended_at=d.last_ended_at,
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

    async def _consume_source_loop(self, method_id: str, source: TraceSource) -> None:
        """Drains one source's `events()` for as long as it runs. Ends
        cleanly when `_stop_source_locked` stops/cancels it. If the
        iterator ends or raises on its own (not via cancellation --
        unreachable for `FileTailTraceSource`, which only ever ends via
        explicit `stop()`, but a future source type might), the entry is
        marked `error` rather than silently removed, so it's still visible
        in `GET /runtime/config` / the stats message instead of vanishing."""
        try:
            async for event in source.events():
                await self._process_event(event, method_id=method_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - this source's own stream failing must not affect other sources
            logger.exception("runtime: consume loop for %s ended unexpectedly", method_id)
            async with self._lock:
                running = self._sources.get(method_id)
                if running is not None:
                    running.error = str(exc)
                    running.task = None

    async def _process_event(self, event: TraceEvent, *, method_id: str) -> None:
        """Resolve + apply one event. Never lets a bug here kill the
        calling `_consume_source_loop`."""
        try:
            async with self._lock:
                running = self._sources.get(method_id)
                if running is not None:
                    running.events_received += 1

                # Task 23 de-dup: an exact (span_id, phase) repeat -- e.g.
                # the same span arriving live from the file tailer AND
                # completed from OTLP -- is dropped before it ever reaches
                # resolve()/apply(). Counted as received (the bytes did
                # arrive) but NOT as ignored: it isn't unresolved, it's a
                # duplicate of something already applied successfully.
                dedup_key = (event.span_id, event.phase)
                if dedup_key in self._seen_span_phases:
                    return
                self._seen_span_phases[dedup_key] = None
                if len(self._seen_span_phases) > _DEDUP_CACHE_SIZE:
                    self._seen_span_phases.popitem(last=False)

                result = self._index.resolve(event)
                if isinstance(result, Unresolved):
                    self.dead_letter.record(event, result.reason)
                    if running is not None:
                        running.events_ignored += 1
                    logger.info(
                        "runtime: unresolved event kind=%s ref=%r reason=%s",
                        event.kind,
                        event.ref,
                        result.reason,
                    )
                    return
                deltas = await self.state_store.apply(event, result)
                self._events_resolved += 1
                for delta in deltas:
                    self._pending[delta.node_id] = delta
        except Exception:  # noqa: BLE001 - one bad event must not stop the whole stream
            logger.exception(
                "runtime: failed to process trace event kind=%s ref=%r", event.kind, event.ref
            )

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
                            last_ended_at=delta.last_ended_at,
                        )
                    )
            except Exception:  # noqa: BLE001 - a bad flush must not kill future flushes
                logger.exception("runtime: batch flush failed")

    async def _dead_letter_stats_loop(self) -> None:
        while True:
            await asyncio.sleep(self._dead_letter_stats_interval_seconds)
            try:
                dropped, parse_errors, non_trace = self._aggregate_source_stats()
                sources = await self.source_statuses()
                self._publish_nowait(
                    DeadLetterStatsMessage(
                        count=self.dead_letter.stats().count,
                        dropped_count=dropped,
                        parse_error_count=parse_errors,
                        non_trace_count=non_trace,
                        events_resolved=self._events_resolved,
                        sources=sources,
                    )
                )
            except Exception:  # noqa: BLE001 - a bad stats publish must not kill future ones
                logger.exception("runtime: dead-letter stats publish failed")

    async def _orphan_sweep_loop(self) -> None:
        """Every `_ORPHAN_SWEEP_INTERVAL_SECONDS`, evict spans stuck `active`
        past `self._orphaned_span_ttl_seconds` -- see `RuntimeStateStore.
        sweep_orphaned_spans` for why (a target project that crashes mid-span
        never sends the matching `end`/`error`, so without this the node
        stays `active` forever).

        Holds `self._lock` around BOTH the sweep AND the `_pending` update,
        matching `_process_event`'s pattern -- see the module docstring on
        why one lock covers "one full operation," not one per structure:
        without it, a `update_graph()` reset racing this sweep could land a
        stale delta (for a node the fresh graph no longer has) into
        `_pending` in the gap between the two. Logging happens AFTER
        releasing the lock -- it's not part of the invariant the lock
        protects, so there's no reason to hold it longer than necessary.
        """
        while True:
            await asyncio.sleep(self._orphan_sweep_interval_seconds)
            try:
                async with self._lock:
                    deltas, evicted = await self.state_store.sweep_orphaned_spans(
                        self._orphaned_span_ttl_seconds
                    )
                    for delta in deltas:
                        self._pending[delta.node_id] = delta
                for span in evicted:
                    logger.info(
                        "runtime: evicted orphaned span node_id=%s span_id=%s age=%.1fs",
                        span.node_id,
                        span.span_id,
                        span.age_seconds,
                    )
            except Exception:  # noqa: BLE001 - a bad sweep must not kill future sweeps
                logger.exception("runtime: orphaned-span sweep failed")

    def _aggregate_source_stats(self) -> tuple[int, int, int]:
        """`(dropped_count, parse_error_count, non_trace_count)`, summed
        across every currently-running source that tracks these (there may
        be several once the Setup screen can turn on more than one method).

        Not part of the `TraceSource` Protocol -- specific to each
        implementation, same as Task 21's single-source version this
        generalizes. `isinstance` (not duck-typing) so a rename of
        `FileTailStats`'s fields fails mypy here instead of silently
        reporting zeros. `OtlpHttpTraceSource` has no concept of "non-trace
        lines" (there's no log file to mix foreign output into) -- it only
        contributes to `dropped`/`parse_errors`.

        Reads `self._sources` without the lock, same tradeoff Task 21's
        version made: these counters only ever grow, and iterating a dict
        with no `await` inside the loop is atomic under asyncio's
        cooperative scheduling -- at worst a one-tick-stale number, never
        a torn read.
        """
        dropped = parse_errors = non_trace = 0
        for running in self._sources.values():
            if isinstance(running.source, FileTailTraceSource):
                stats = running.source.stats()
                dropped += stats.dropped_count
                parse_errors += stats.parse_error_count
                non_trace += stats.non_trace_count
            elif isinstance(running.source, OtlpHttpTraceSource):
                otlp_stats = running.source.stats()
                dropped += otlp_stats.dropped_count
                parse_errors += otlp_stats.parse_error_count
        return dropped, parse_errors, non_trace
