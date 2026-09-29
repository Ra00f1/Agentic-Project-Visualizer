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
everywhere the task's prose says "run"; only the field names below
(`active_spans`, `errored_span_id`) changed from the task's `active_runs`
wording — the state machine's actual transitions are otherwise unchanged:

    idle    + start(s) -> active   (active_spans = {s})
    active  + start(s) -> active   (active_spans |= {s})           # no delta
    active  + end(s)   -> active if active_spans remain else idle
    active  + error(s) -> errored  (active_spans -= {s}, last_error set)
    errored + start(s) -> errored  (active_spans |= {s})            # no delta
    errored + end(s)   -> idle iff active_spans empties AND s was the span
                           that most recently caused the error
    errored + error(s) -> errored  (last_error updated)

"Stays red until the span that broke it finishes cleanly" is the point of the
errored state — a flood of concurrent successful calls on other spans must
not paper over one that's still broken.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from agentic_visualizer.runtime.messages import NodeStatus, RuntimeErrorInfo
from agentic_visualizer.runtime.node_index import Resolved
from agentic_visualizer.trace.events import TraceEvent


@dataclass(slots=True)
class NodeRuntimeState:
    """Mutable, single-node runtime state. Owned exclusively by `RuntimeStateStore`."""

    node_id: str
    status: NodeStatus = "idle"
    active_spans: set[str] = field(default_factory=set)
    run_count: int = 0
    """Lifetime count of distinct spans (invocations) that have started on this node."""
    error_count: int = 0
    """Lifetime count of error events on this node."""
    errored_span_id: str | None = None
    """The span_id whose error most recently put this node in `errored` —
    needed to implement "stays red until THAT invocation finishes", not just any."""
    last_error: RuntimeErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class NodeDelta:
    """A point-in-time snapshot of one node's runtime state, ready for the wire."""

    node_id: str
    status: NodeStatus
    run_count: int
    error_count: int
    last_error: RuntimeErrorInfo | None


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
    )


def _apply_one(state: NodeRuntimeState, event: TraceEvent) -> bool:
    """Mutate `state` per `event`. Returns True iff the change is worth a delta.

    A delta fires when `status` actually transitions, or on every `error`
    phase regardless of whether status was already `errored` -- a second
    error's message/traceback is new, user-visible information for the
    detail panel even when the status dot doesn't change color.

    Concurrency is tracked by `event.span_id`, not `event.run_id` -- see this
    module's docstring for why `run_id` (process-scoped, shared by every
    event a target project ever emits) would be wrong here.
    """
    before_status = state.status

    if event.phase == "start":
        is_new_span = event.span_id not in state.active_spans
        state.active_spans.add(event.span_id)
        if is_new_span:
            state.run_count += 1
        if state.status == "idle":
            state.status = "active"
        # active/errored: status intentionally unchanged; active_spans still tracked.

    elif event.phase == "end":
        had_span = event.span_id in state.active_spans
        state.active_spans.discard(event.span_id)
        if state.status == "errored":
            if not state.active_spans and state.errored_span_id == event.span_id:
                state.status = "idle"
                state.errored_span_id = None
        elif had_span:
            state.status = "active" if state.active_spans else "idle"

    elif event.phase == "error":
        state.active_spans.discard(event.span_id)
        state.error_count += 1
        state.status = "errored"
        state.errored_span_id = event.span_id
        state.last_error = RuntimeErrorInfo.from_event(event)

    return state.status != before_status or event.phase == "error"
