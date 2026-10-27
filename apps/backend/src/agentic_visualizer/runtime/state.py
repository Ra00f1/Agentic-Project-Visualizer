"""RuntimeStateStore — per-node runtime status, derived from resolved trace events.

`NodeRuntimeState` is internal, mutable, single-writer-at-a-time state (guarded
by `_lock`) — deliberately NOT a wire-format Pydantic model. `NodeDelta` is
the read-only snapshot of one node's state that actually crosses the
WebSocket boundary (as `messages.RuntimeDeltaMessage`); keeping them separate
means `RuntimeStateStore` never has to worry about someone mutating a value
it handed out.

**Deviation from Task 3's `## Design`, flagged up front:** the state-machine
table there writes transitions as `start(run_r)`/`end(run_r)`, tracking
concurrency by `run_id`. That's the wrong field. Per `apv_trace`
(`runtime-demo-project/apv_trace/__init__.py`'s `configure()`), `run_id` is
generated **once per process** — every event a single instrumented process
ever emits carries the same `run_id`. The field that's actually unique per
invocation is `span_id` (freshly generated in `_span()` for every traced
call). Tracking "is this node currently in flight" by `run_id` would treat
two genuinely concurrent calls to the same node (e.g. a server handling two
requests at once, or `asyncio.gather` calling the same tool twice) as *one*
call — the first `end` would flip the node to idle while the second call is
still running, and it would never be corrected. This module uses `span_id`
everywhere the task's prose says "run".

**Task 24 (D1): errors now clear on a later successful run, not just the
exact span that failed.** The ORIGINAL rule here ("stays red until the span
that broke it finishes cleanly") was unimplementable as written: a span that
fails emits `error`, never `end` — so "the span that broke it finishes
cleanly" can never happen for that span, and a node that ever errors once
stayed red forever (confirmed live, Task 24's Step 0: a workflow and agent
node stayed red through an entire SECOND, completely clean run). The real
intent — a flood of concurrent successful calls on other spans must not
paper over one that's still broken — only needed "finishes cleanly"
to mean "a span that started AFTER the failure," not "that exact span":

    idle    + start(s) -> active   (active_spans[s] = (now, run_count))
    active  + start(s) -> active   (active_spans[s] = (now, run_count))
    active  + end(s)   -> active if active_spans remain, else idle/errored
                           (errored iff an error hasn't cleared -- see below)
    active  + error(s) -> active if active_spans remain, else errored
    errored + start(s) -> active   (ALWAYS -- a node gets a fresh shot at
                                     recovery on every new call, not just
                                     spans that happen to still be in flight)
    errored + end(s)   -> clears the error IFF s started strictly after the
                           error (s's own run_count snapshot > the run_count
                           snapshot taken when the error happened); status
                           then becomes idle if active_spans also empties,
                           else stays active
    errored + error(s) -> errored  (error_seq refreshed to the current
                                     run_count, last_error updated)

`error_seq` (on `NodeRuntimeState`) is that run_count snapshot — `None` when
no error is currently unresolved. A span concurrent WITH the failing one
(already running when it failed) has a start-time run_count <= error_seq,
so it can never accidentally clear an error that's still live — only a span
that started afterward (the node's "next run") can. `errored_span_id` is
gone: there's no longer one specific span whose `end` is privileged, any
span that started later than the error qualifies.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime

from agentic_visualizer.runtime.messages import NodeStatus, RuntimeErrorInfo
from agentic_visualizer.runtime.node_index import Resolved
from agentic_visualizer.trace.events import TraceEvent


@dataclass(slots=True)
class NodeRuntimeState:
    """Mutable, single-node runtime state. Owned exclusively by `RuntimeStateStore`."""

    node_id: str
    status: NodeStatus = "idle"
    active_spans: dict[str, tuple[float, int]] = field(default_factory=dict)
    """span_id -> (`time.monotonic()` when its `start` event was applied,
    `run_count` at that same moment -- Task 24's `start_seq`, read by `end`
    to decide whether this span started after the node's most recent error).
    `time.monotonic()`, not `time.time()`, so an NTP jump or DST change can't
    retroactively make a span look (or stop looking) orphaned -- see
    `RuntimeStateStore.sweep_orphaned_spans`, the only reader of that half
    of the tuple."""
    run_count: int = 0
    """Lifetime count of distinct spans (invocations) that have started on this node."""
    error_count: int = 0
    """Lifetime count of error events on this node."""
    error_seq: int | None = None
    """Task 24 (D1): `run_count` snapshot taken at the node's most recent
    still-unresolved error. `None` means no error is currently blocking
    recovery. Cleared back to `None` when a span whose own start-time
    `run_count` snapshot is STRICTLY GREATER than this -- i.e. a span that
    started after the failure, not one merely still in flight when it
    happened -- ends successfully. See this module's own docstring for the
    full reasoning and `_apply_one`'s "end" branch for the comparison."""
    last_error: RuntimeErrorInfo | None = None
    last_ended_at: datetime | None = None
    """When the most recent `end`/`error` event for this node happened,
    taken from `event.ts` (Task 23's "just ran" flash) -- set on EVERY
    end/error, not just ones that also flip `status`, so a span that
    completes without changing status (e.g. another span is still active
    on the same node) still produces a delta the frontend can flash on."""


@dataclass(frozen=True, slots=True)
class NodeDelta:
    """A point-in-time snapshot of one node's runtime state, ready for the wire."""

    node_id: str
    status: NodeStatus
    run_count: int
    error_count: int
    last_error: RuntimeErrorInfo | None
    last_ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class EvictedSpan:
    """One span `sweep_orphaned_spans` evicted -- enough for the caller to log it."""

    node_id: str
    span_id: str
    age_seconds: float


class RuntimeStateStore:
    """Owns every node's `NodeRuntimeState`, derives deltas from resolved events."""

    def __init__(self) -> None:
        self._states: dict[str, NodeRuntimeState] = {}
        self._lock = asyncio.Lock()

    async def apply(self, event: TraceEvent, resolved: Resolved) -> list[NodeDelta]:
        """Apply `event` to every node `resolved` points at; return the deltas worth publishing.

        A single event can touch more than one node (the module_qualname+bridge
        resolution strategy deliberately resolves to both a function AND the
        tool(s) that wrap it), so this returns a list, not a single delta.
        """
        deltas: list[NodeDelta] = []
        async with self._lock:
            for node_id in resolved.node_ids:
                state = self._states.get(node_id)
                if state is None:
                    # get-then-set, not setdefault: setdefault evaluates its
                    # default argument unconditionally, allocating a throwaway
                    # NodeRuntimeState (and its empty set()) on every event for
                    # an already-tracked node -- the overwhelmingly common case.
                    state = NodeRuntimeState(node_id=node_id)
                    self._states[node_id] = state
                if _apply_one(state, event):
                    deltas.append(_to_delta(state))
        return deltas

    async def reset(self) -> None:
        """Drop all tracked state — called when the graph refreshes (new NodeIndex)."""
        async with self._lock:
            self._states.clear()

    async def sweep_orphaned_spans(
        self, ttl_seconds: float, *, now: float | None = None
    ) -> tuple[list[NodeDelta], list[EvictedSpan]]:
        """Evict any span that's been active longer than `ttl_seconds`.

        Protects against a target project crashing (or being killed) mid-span:
        without this, a node whose `end`/`error` event never arrives stays
        `active` forever. Eviction is NEVER treated as "finished successfully"
        -- a timed-out span never clears `error_seq` (see `_apply_one`'s
        docstring: an error clears only on a genuine `end`), and if `status`
        was `active` only because THIS span was still in flight while an
        EARLIER, different span's error is still unresolved, eviction drops
        it back to `errored`, not `idle` -- an errored node silently going
        back to idle because an unrelated stuck span timed out would hide a
        real failure just as much as the span that actually caused the
        error would.

        `now` is injectable (defaults to `time.monotonic()`) so tests can
        simulate elapsed time without a real sleep -- see
        `tests/runtime/test_state_transitions.py`.
        """
        current_time = now if now is not None else time.monotonic()
        deltas: list[NodeDelta] = []
        evicted: list[EvictedSpan] = []
        async with self._lock:
            for state in self._states.values():
                stale_span_ids = [
                    span_id
                    for span_id, (started_at, _start_seq) in state.active_spans.items()
                    if current_time - started_at > ttl_seconds
                ]
                if not stale_span_ids:
                    continue
                for span_id in stale_span_ids:
                    started_at, _start_seq = state.active_spans.pop(span_id)
                    evicted.append(
                        EvictedSpan(
                            node_id=state.node_id,
                            span_id=span_id,
                            age_seconds=current_time - started_at,
                        )
                    )
                if not state.active_spans and state.status == "active":
                    state.status = _idle_or_errored(state)
                    deltas.append(_to_delta(state))
        return deltas, evicted

    def snapshot(self) -> list[NodeDelta]:
        """Every currently-tracked node's state, for a client that just connected.

        Synchronous and unlocked on purpose: this method itself has no
        `await` in it, so once a task starts running it, the event loop
        cannot switch to any other task (including one running `apply`/
        `reset`) until it returns -- it's atomic with respect to the whole
        program, not just with respect to this store's own other methods.
        """
        return [_to_delta(state) for state in self._states.values()]


def _to_delta(state: NodeRuntimeState) -> NodeDelta:
    return NodeDelta(
        node_id=state.node_id,
        status=state.status,
        run_count=state.run_count,
        error_count=state.error_count,
        last_error=state.last_error,
        last_ended_at=state.last_ended_at,
    )


def _apply_one(state: NodeRuntimeState, event: TraceEvent) -> bool:
    """Mutate `state` per `event`. Returns True iff the change is worth a delta.

    A delta fires when `status` actually transitions, on every `error`
    phase regardless of whether status was already `errored` (a second
    error's message/traceback is new, user-visible information for the
    detail panel even when the status dot doesn't change color), or
    (Task 23) whenever `last_ended_at` moves -- every completed span is
    worth a delta now, even one that doesn't flip `status` (e.g. a second
    concurrent span on the same node is still active), so the frontend's
    "just ran" flash fires for every finished call, live or not.

    Concurrency is tracked by `event.span_id`, not `event.run_id` -- see this
    module's docstring for why `run_id` (process-scoped, shared by every
    event a target project ever emits) would be wrong here. Task 24 (D1):
    see this module's own docstring for the `error_seq`/`start_seq` recovery
    rule `start`/`end` implement below.
    """
    before_status = state.status
    before_ended_at = state.last_ended_at

    if event.phase == "start":
        existing = state.active_spans.get(event.span_id)
        if existing is None:
            state.run_count += 1
            start_seq = state.run_count
        else:
            # Duplicate start for a span already tracked -- keep its
            # ORIGINAL start_seq (only restart the orphan-sweep TTL clock).
            # Re-snapshotting start_seq to the CURRENT run_count here would
            # let this span's later `end` compare against a start_seq newer
            # than when it actually started, wrongly clearing an error this
            # span was genuinely concurrent with (not one that came after).
            _, start_seq = existing
        state.active_spans[event.span_id] = (time.monotonic(), start_seq)
        # Always active, even from errored -- a node gets a fresh shot at
        # recovery on every new call, not just spans already in flight.
        state.status = "active"

    elif event.phase == "end":
        popped = state.active_spans.pop(event.span_id, None)
        state.last_ended_at = event.ts
        if popped is not None:
            _started_at, start_seq = popped
            if state.error_seq is not None and start_seq > state.error_seq:
                state.error_seq = None
        state.status = "active" if state.active_spans else _idle_or_errored(state)

    elif event.phase == "error":
        state.active_spans.pop(event.span_id, None)
        state.error_count += 1
        state.error_seq = state.run_count
        state.last_error = RuntimeErrorInfo.from_event(event)
        state.last_ended_at = event.ts
        state.status = "active" if state.active_spans else "errored"

    return (
        state.status != before_status
        or event.phase == "error"
        or state.last_ended_at != before_ended_at
    )


def _idle_or_errored(state: NodeRuntimeState) -> NodeStatus:
    """With nothing in flight: `errored` iff an error hasn't cleared yet, else `idle`."""
    return "errored" if state.error_seq is not None else "idle"
