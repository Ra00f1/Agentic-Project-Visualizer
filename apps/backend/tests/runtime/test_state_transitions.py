"""Table-driven state-transition tests for RuntimeStateStore -- one case per row
in Task 3's `## Design` state-machine table.
"""

from __future__ import annotations

import itertools
import time
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from agentic_visualizer.runtime.node_index import Resolved
from agentic_visualizer.runtime.state import NodeDelta, NodeRuntimeState, RuntimeStateStore
from agentic_visualizer.trace.events import TraceEvent, TraceRef

_TS = datetime(2026, 1, 1, tzinfo=UTC)
_NODE_ID = "code:x.py:1:foo"

# Every call to _event() gets a DISTINCT, strictly increasing `ts` (Task 23):
# a shared hardcoded timestamp would mask `last_ended_at`'s own change
# detection in `_apply_one` -- two end/error events that happen to carry the
# identical `ts` look like "no change" even though real trace events never
# share a timestamp. One counter, module-wide, is enough: these tests don't
# care about the actual time values, only that each event's is unique.
_ts_offsets = itertools.count()


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
        ts=_TS + timedelta(milliseconds=next(_ts_offsets)),
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

    # r2 still active -- status doesn't change, but last_ended_at does
    # (Task 23: every end is worth a delta, for the "just ran" flash).
    assert len(deltas) == 1
    assert deltas[0].status == "active"


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
async def test_errored_start_always_goes_active_task24_case1() -> None:
    """Task 24 (D1), case 1 part 1: a node gets a fresh shot at recovery on
    its very next call, not just spans that happened to still be in flight
    when the error occurred -- this is the fix for the confirmed bug
    (Step 0: a node that ever errors once stayed red forever, since a
    failed span emits `error`, never `end`, so the OLD "stays red until
    THAT span finishes cleanly" rule could never actually clear)."""
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))
    await _apply(store, _event(span="r1", phase="error", with_error=True))

    deltas = await _apply(store, _event(span="r2", phase="start"))

    assert len(deltas) == 1
    assert deltas[0].status == "active"


@pytest.mark.asyncio
async def test_errored_recovers_when_a_later_span_ends_ok_task24_case1() -> None:
    """Task 24 (D1), case 1 part 2: continuing case 1 -- that later span
    (started AFTER the error, a different span_id -- a real failed call
    never gets a trailing `end` for its OWN span_id) ending successfully
    clears the error and the node goes idle."""
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))
    await _apply(store, _event(span="r1", phase="error", with_error=True))
    await _apply(store, _event(span="r2", phase="start"))

    deltas = await _apply(store, _event(span="r2", phase="end"))

    assert len(deltas) == 1
    assert deltas[0].status == "idle"


@pytest.mark.asyncio
async def test_errored_while_another_span_active_then_that_span_ends_task24_case2() -> None:
    """Task 24 (D1), case 2: A errors while B is still active -> active
    (the node isn't fully "down" while something else is genuinely still
    running); B -- already in flight when A failed, not a later run --
    ending successfully must NOT paper over A's failure."""
    store = RuntimeStateStore()
    await _apply(store, _event(span="a", phase="start"))
    await _apply(store, _event(span="b", phase="start"))

    error_deltas = await _apply(store, _event(span="a", phase="error", with_error=True))
    assert len(error_deltas) == 1
    assert error_deltas[0].status == "active"  # b is still running

    end_deltas = await _apply(store, _event(span="b", phase="end"))
    assert len(end_deltas) == 1
    assert end_deltas[0].status == "errored"  # b started before a's error -- doesn't clear it


@pytest.mark.asyncio
async def test_next_run_after_case2_clears_the_error_task24_case3() -> None:
    """Task 24 (D1), case 3: continuing case 2 -- C, starting AFTER A's
    error, ending successfully is what finally clears it."""
    store = RuntimeStateStore()
    await _apply(store, _event(span="a", phase="start"))
    await _apply(store, _event(span="b", phase="start"))
    await _apply(store, _event(span="a", phase="error", with_error=True))
    await _apply(store, _event(span="b", phase="end"))  # still errored (case 2)

    await _apply(store, _event(span="c", phase="start"))
    deltas = await _apply(store, _event(span="c", phase="end"))

    assert len(deltas) == 1
    assert deltas[0].status == "idle"


@pytest.mark.asyncio
async def test_error_then_start_then_error_again_task24_case4() -> None:
    """Task 24 (D1), case 4: a node that errors, gets a fresh start, and
    errors again stays errored with both failures counted."""
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))
    await _apply(store, _event(span="r1", phase="error", with_error=True))
    await _apply(store, _event(span="r2", phase="start"))

    deltas = await _apply(store, _event(span="r2", phase="error", with_error=True))

    assert len(deltas) == 1
    assert deltas[0].status == "errored"
    assert deltas[0].error_count == 2


@pytest.mark.asyncio
async def test_duplicate_start_does_not_corrupt_start_seq_task24_regression() -> None:
    """Regression test (found during Task 24's own code review, by two
    independent reviewers): a DUPLICATE `start` for a span already in
    `active_spans` -- e.g. a retried delivery, or the same span arriving
    from two trace sources per Task 23's dedup comment in channel.py --
    must preserve its ORIGINAL start_seq, not re-snapshot it to the
    CURRENT run_count. Overwriting it would let a span that was genuinely
    concurrent with an error (and so should never be able to clear it,
    per case 2) wrongly clear it, just because an unrelated LATER span
    happened to start before the duplicate start arrived."""
    store = RuntimeStateStore()
    await _apply(store, _event(span="a", phase="start"))  # run_count=1, a's start_seq=1
    await _apply(store, _event(span="c", phase="start"))  # run_count=2, c's start_seq=2
    await _apply(store, _event(span="b", phase="start"))  # run_count=3, b's start_seq=3
    await _apply(store, _event(span="b", phase="error", with_error=True))  # error_seq=3; a, c still active
    await _apply(store, _event(span="d", phase="start"))  # run_count=4 -- unrelated, started AFTER the error

    # A duplicate delivery of a's own start -- a is already in active_spans.
    # Without the fix, this wrongly re-snapshots a's start_seq to 4.
    await _apply(store, _event(span="a", phase="start"))

    await _apply(store, _event(span="a", phase="end"))  # must NOT clear -- a's real start_seq (1) <= error_seq (3)
    await _apply(store, _event(span="c", phase="end"))  # must NOT clear either -- c's start_seq (2) <= error_seq (3)

    # d never gets a real `end` -- only a genuine `end` from a span that
    # started after the error (d, here) could legitimately clear it, and
    # d's own never arrives, so the node must still show the unresolved
    # error once everything empties.
    deltas, evicted = await store.sweep_orphaned_spans(ttl_seconds=5.0, now=time.monotonic() + 10.0)

    assert len(evicted) == 1
    assert evicted[0].span_id == "span-d"
    assert len(deltas) == 1
    assert deltas[0].status == "errored"  # not idle -- a's duplicate start must not have corrupted its start_seq


@pytest.mark.asyncio
async def test_error_absorbed_by_a_later_span_while_an_older_one_still_runs() -> None:
    """Known, intentional trade-off found during Task 24's own code review
    (not a bug to fix without a new design decision): if a LATER span
    (started after the error) finishes successfully WHILE an OLDER,
    already-concurrent span is still running, the error clears right
    then -- even though the node's `status` never visibly showed
    "errored" at all (it was "active" the whole time, per case 2's own
    rule: active whenever anything is in flight). `error_count`/
    `last_error` still recorded the failure -- only the status DOT's
    transient red never appeared. This is the accepted consequence of
    "a span that started after the failure proves the node has
    recovered" not waiting for every older, unrelated span to drain
    first. Pinned here so a future change to this behavior is a
    deliberate decision, not an accidental regression discovered by a
    confused bug report."""
    store = RuntimeStateStore()
    await _apply(store, _event(span="older", phase="start"))  # run_count=1 -- still running throughout
    await _apply(store, _event(span="failing", phase="start"))  # run_count=2
    error_deltas = await _apply(store, _event(span="failing", phase="error", with_error=True))
    assert error_deltas[0].status == "active"  # never "errored" -- older is still in flight

    await _apply(store, _event(span="later", phase="start"))  # run_count=3 -- started AFTER the error
    clear_deltas = await _apply(store, _event(span="later", phase="end"))
    # error_seq clears right here -- "older" (start_seq=1) is still running,
    # unaffected by this, but the error is already resolved by the time it finishes.
    assert clear_deltas[0].status == "active"  # older still running; error already cleared underneath

    final_deltas = await _apply(store, _event(span="older", phase="end"))
    assert final_deltas[0].status == "idle"  # not "errored" -- the status dot never showed red at all


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
    # call-b (same run_id, different span) is still active -- status stays
    # the same, but last_ended_at still moves (Task 23: every end is worth
    # a delta).
    assert len(deltas) == 1
    assert deltas[0].status == "active"

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
    assert state.active_spans == {}
    assert state.run_count == 0
    assert state.error_count == 0
    assert state.error_seq is None


@pytest.mark.asyncio
async def test_sweep_evicts_a_span_past_ttl_and_flips_active_node_to_idle() -> None:
    """Task 9 Scope D: a `start` with no matching `end`/`error` must not keep
    a node `active` forever. `now` is injected (not a real sleep) to
    simulate elapsed time -- see `sweep_orphaned_spans`'s own docstring."""
    store = RuntimeStateStore()
    await _apply(store, _event(span="stuck", phase="start"))
    assert store.snapshot()[0].status == "active"

    deltas, evicted = await store.sweep_orphaned_spans(ttl_seconds=5.0, now=time.monotonic() + 10.0)

    assert len(deltas) == 1
    assert deltas[0].node_id == _NODE_ID
    assert deltas[0].status == "idle"
    assert len(evicted) == 1
    assert evicted[0].node_id == _NODE_ID
    assert evicted[0].span_id == "span-stuck"
    assert evicted[0].age_seconds >= 10.0


@pytest.mark.asyncio
async def test_sweep_leaves_spans_within_ttl_untouched() -> None:
    store = RuntimeStateStore()
    await _apply(store, _event(span="fresh", phase="start"))

    deltas, evicted = await store.sweep_orphaned_spans(ttl_seconds=5.0, now=time.monotonic())

    assert deltas == []
    assert evicted == []
    assert store.snapshot()[0].status == "active"  # untouched


@pytest.mark.asyncio
async def test_sweep_drops_active_back_to_errored_not_idle_when_its_last_span_times_out() -> None:
    """Task 24 (D1) changed what status is BEFORE this sweep runs: under the
    new rule, a node with one span errored and a second still genuinely
    running is `active` (case 2), not `errored` -- unresolved error or not,
    something is really in flight. If THAT remaining span times out instead
    of ending normally, eviction must drop the node back to `errored` (the
    unresolved failure is still real), not `idle` -- a stuck span timing
    out must not silently paper over a failure any more than a stuck span
    finishing cleanly would. Note `error`'s own span is already popped from
    `active_spans` by `_apply_one` the moment it fires -- this test uses a
    SECOND, concurrent span as the one that actually gets evicted."""
    store = RuntimeStateStore()
    await _apply(store, _event(span="broke-it", phase="start"))
    await _apply(store, _event(span="still-running", phase="start"))
    error_deltas = await _apply(store, _event(span="broke-it", phase="error", with_error=True))
    assert error_deltas[0].status == "active"  # still-running keeps it active, not errored yet

    deltas, evicted = await store.sweep_orphaned_spans(ttl_seconds=5.0, now=time.monotonic() + 10.0)

    assert len(deltas) == 1  # active -> errored IS a real transition, worth a delta
    assert deltas[0].status == "errored"
    assert len(evicted) == 1
    assert evicted[0].span_id == "span-still-running"
    snapshot = store.snapshot()[0]
    assert snapshot.status == "errored"


@pytest.mark.asyncio
async def test_sweep_on_an_already_errored_node_with_nothing_in_flight_is_a_no_op_task24_case5() -> None:
    """Task 24 (D1), case 5: a node that's already `errored` with an empty
    `active_spans` (the span that broke it already ended/errored, nothing
    else was ever concurrent) has nothing for the sweep to evict -- it
    stays errored, untouched, no delta."""
    store = RuntimeStateStore()
    await _apply(store, _event(span="r1", phase="start"))
    await _apply(store, _event(span="r1", phase="error", with_error=True))
    assert store.snapshot()[0].status == "errored"

    deltas, evicted = await store.sweep_orphaned_spans(ttl_seconds=5.0, now=time.monotonic() + 10.0)

    assert deltas == []
    assert evicted == []
    assert store.snapshot()[0].status == "errored"
