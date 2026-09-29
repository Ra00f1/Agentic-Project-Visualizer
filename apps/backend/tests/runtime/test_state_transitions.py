"""Table-driven state-transition tests for RuntimeStateStore -- one case per row
in Task 3's `## Design` state-machine table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest

from agentic_visualizer.runtime.node_index import Resolved
from agentic_visualizer.runtime.state import NodeDelta, NodeRuntimeState, RuntimeStateStore
from agentic_visualizer.trace.events import TraceEvent, TraceRef

_TS = datetime(2026, 1, 1, tzinfo=UTC)
_NODE_ID = "code:x.py:1:foo"


def _event(
    *,
    span: str,
    phase: Literal["start", "end", "error"],
    with_error: bool = False,
    run_id: str = "shared-process-run-id",
) -> TraceEvent:
    """`run_id` defaults to a constant shared across every call, matching real
    `apv_trace` behavior (one run_id per process) -- `span` is what actually
    varies per invocation. Concurrency in RuntimeStateStore is tracked by
    span_id precisely because run_id does NOT vary per call; see state.py's
    module docstring."""
    meta = {}
    if with_error:
        meta = {"error": {"type": "RuntimeError", "message": "boom", "traceback": "..."}}
    return TraceEvent(
        v=1,
        ts=_TS,
        run_id=run_id,
        span_id=f"span-{span}",
        phase=phase,
        kind="tool",
        ref=TraceRef(name="foo", module="pkg.mod"),
        meta=meta,
    )


async def _apply(store: RuntimeStateStore, event: TraceEvent) -> list[NodeDelta]:
    return await store.apply(event, Resolved([_NODE_ID], strategy="test"))


@pytest.mark.asyncio
async def test_idle_start_becomes_active() -> None:
    store = RuntimeStateStore()
    deltas = await _apply(store, _event(span="r1", phase="start"))

    assert len(deltas) == 1
    assert deltas[0].status == "active"
    assert deltas[0].run_count == 1


@pytest.mark.asyncio
async def test_active_start_stays_active_no_delta() -> None:
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))

    deltas = await _apply(store, _event(span="r2", phase="start"))

    assert deltas == []  # status didn't change -- no delta, per Task 3's own example


@pytest.mark.asyncio
async def test_active_end_stays_active_when_another_run_remains() -> None:
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))
    await _apply(store, _event(span="r2", phase="start"))

    deltas = await _apply(store, _event(span="r1", phase="end"))

    assert deltas == []  # r2 still active -- no visible status change


@pytest.mark.asyncio
async def test_active_end_becomes_idle_when_no_runs_remain() -> None:
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))

    deltas = await _apply(store, _event(span="r1", phase="end"))

    assert len(deltas) == 1
    assert deltas[0].status == "idle"


@pytest.mark.asyncio
async def test_active_error_becomes_errored() -> None:
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))

    deltas = await _apply(store, _event(span="r1", phase="error", with_error=True))

    assert len(deltas) == 1
    assert deltas[0].status == "errored"
    assert deltas[0].error_count == 1
    assert deltas[0].last_error is not None
    assert deltas[0].last_error.type == "RuntimeError"


@pytest.mark.asyncio
async def test_errored_start_stays_errored_no_delta() -> None:
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))
    await _apply(store, _event(span="r1", phase="error", with_error=True))

    deltas = await _apply(store, _event(span="r2", phase="start"))

    assert deltas == []  # stays red, but tracked as running


@pytest.mark.asyncio
async def test_errored_end_of_erroring_run_becomes_idle_when_no_runs_remain() -> None:
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))
    await _apply(store, _event(span="r1", phase="error", with_error=True))

    deltas = await _apply(store, _event(span="r1", phase="end"))

    assert len(deltas) == 1
    assert deltas[0].status == "idle"


@pytest.mark.asyncio
async def test_errored_end_of_different_run_stays_errored() -> None:
    """A concurrent run finishing cleanly must not paper over the one that broke."""
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))
    await _apply(store, _event(span="r2", phase="start"))
    await _apply(store, _event(span="r1", phase="error", with_error=True))

    deltas = await _apply(store, _event(span="r2", phase="end"))

    assert deltas == []  # r2 wasn't the erroring run -- stays errored, no delta


@pytest.mark.asyncio
async def test_errored_end_stays_errored_when_other_runs_remain() -> None:
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))
    await _apply(store, _event(span="r2", phase="start"))
    await _apply(store, _event(span="r1", phase="error", with_error=True))

    deltas = await _apply(store, _event(span="r1", phase="end"))

    assert deltas == []  # r1 was the erroring run, but r2 is still active


@pytest.mark.asyncio
async def test_errored_error_stays_errored_but_updates_last_error() -> None:
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))
    await _apply(store, _event(span="r1", phase="error", with_error=True))

    deltas = await _apply(store, _event(span="r1", phase="error", with_error=True))

    # Status didn't change, but error phases always surface -- new error
    # info is user-visible even when the dot's already red.
    assert len(deltas) == 1
    assert deltas[0].status == "errored"
    assert deltas[0].error_count == 2


@pytest.mark.asyncio
async def test_concurrent_spans_sharing_one_run_id_are_tracked_independently() -> None:
    """Regression test: apv_trace assigns ONE run_id per process, shared by every
    event it ever emits -- two genuinely concurrent calls to the same node
    (e.g. two in-flight requests hitting the same tool) share run_id but get
    distinct span_ids. Tracking concurrency by run_id would make the first
    call's `end` incorrectly flip the node to idle while the second call is
    still running."""
    store = RuntimeStateStore()
    await _apply(store, _event(span="call-a", phase="start"))  # same run_id as call-b
    await _apply(store, _event(span="call-b", phase="start"))  # (both default to the shared one)

    deltas = await _apply(store, _event(span="call-a", phase="end"))
    assert deltas == []  # call-b (same run_id, different span) is still active

    deltas = await _apply(store, _event(span="call-b", phase="end"))
    assert len(deltas) == 1
    assert deltas[0].status == "idle"  # now both spans are done


@pytest.mark.asyncio
async def test_reset_clears_all_tracked_state() -> None:
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))
    assert store.snapshot() != []

    await store.reset()

    assert store.snapshot() == []


@pytest.mark.asyncio
async def test_bridged_resolution_applies_to_every_resolved_node() -> None:
    """A resolve() that returns multiple node_ids (the bridge case) must update all of them."""
    store = RuntimeStateStore()
    resolved = Resolved(["fn-id", "tool-id"], strategy="module_qualname+bridge")

    deltas = await store.apply(_event(span="r1", phase="start"), resolved)

    assert {d.node_id for d in deltas} == {"fn-id", "tool-id"}
    assert all(d.status == "active" for d in deltas)


def test_default_state_is_idle() -> None:
    state = NodeRuntimeState(node_id="x")
    assert state.status == "idle"
    assert state.active_spans == set()
    assert state.run_count == 0
    assert state.error_count == 0
