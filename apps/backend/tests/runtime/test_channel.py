"""End-to-end tests for RuntimeChannel: TraceSource -> resolve -> apply -> batch -> publish.

Uses a fake TraceSource (same shape as Task 2's test_manager.py fixture)
rather than a real FileTailTraceSource -- this task cares about the
correlation/state/batching/publish pipeline, not file IO.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

import pytest

from agentic_visualizer.domain import Edge, Graph, Node, Provenance
from agentic_visualizer.runtime.channel import RuntimeChannel
from agentic_visualizer.runtime.messages import (
    RuntimeDeltaMessage,
    RuntimeMessage,
    RuntimeResetMessage,
)
from agentic_visualizer.trace.events import TraceEvent, TraceRef
from tests.conftest import ScriptedTraceSource as _ScriptedSource

_TS = datetime(2026, 1, 1, tzinfo=UTC)
_FAST_BATCH_WINDOW = 0.01


def _prov() -> Provenance:
    return Provenance(source="test", source_ref="test:1", scanned_at=_TS)


def _event(*, run_id: str, phase: Literal["start", "end", "error"], name: str = "foo") -> TraceEvent:
    return TraceEvent(
        v=1, ts=_TS, run_id=run_id, span_id=f"span-{run_id}", phase=phase, kind="tool",
        ref=TraceRef(name=name, module="pkg.mod"),
    )


async def _collect_until(
    queue: asyncio.Queue[RuntimeMessage],
    predicate: Callable[[RuntimeMessage], bool],
    timeout: float = 2.0,
) -> RuntimeMessage:
    """Pull messages off `queue` until one satisfies `predicate`; return it."""
    async def _run() -> RuntimeMessage:
        while True:
            message = await queue.get()
            if predicate(message):
                return message

    return await asyncio.wait_for(_run(), timeout=timeout)


@pytest.mark.asyncio
async def test_resolved_events_publish_batched_deltas() -> None:
    node = Node(id="mongo:db.tools:1", type="tool", name="foo", provenance=_prov())
    source = _ScriptedSource([_event(run_id="r1", phase="start")])

    channel = RuntimeChannel(batch_window_seconds=_FAST_BATCH_WINDOW)
    await channel.update_graph(Graph(nodes=[node]))
    await channel.set_source(source)
    subscriber = channel.subscribe()
    await channel.start()
    try:
        message = await _collect_until(subscriber, lambda m: isinstance(m, RuntimeDeltaMessage))
        assert isinstance(message, RuntimeDeltaMessage)
        assert message.node_id == node.id
        assert message.status == "active"
    finally:
        channel.unsubscribe(subscriber)
        await channel.stop()


@pytest.mark.asyncio
async def test_unresolved_events_go_to_dead_letter_not_a_delta() -> None:
    source = _ScriptedSource([_event(run_id="r1", phase="start", name="nothing_matches")])

    channel = RuntimeChannel(batch_window_seconds=_FAST_BATCH_WINDOW)
    await channel.update_graph(Graph())  # empty graph -- nothing can resolve
    await channel.set_source(source)
    await channel.start()
    try:
        # Give the consumer loop a moment to process the one scripted event.
        for _ in range(50):
            if channel.dead_letter.stats().count > 0:
                break
            await asyncio.sleep(0.02)
        assert channel.dead_letter.stats().count == 1
        assert channel.state_store.snapshot() == []
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_update_graph_resets_state_and_publishes_reset() -> None:
    node = Node(id="mongo:db.tools:1", type="tool", name="foo", provenance=_prov())
    source = _ScriptedSource([_event(run_id="r1", phase="start")])

    channel = RuntimeChannel(batch_window_seconds=_FAST_BATCH_WINDOW)
    await channel.update_graph(Graph(nodes=[node]))
    await channel.set_source(source)
    subscriber = channel.subscribe()
    await channel.start()
    try:
        await _collect_until(subscriber, lambda m: isinstance(m, RuntimeDeltaMessage))
        assert channel.state_store.snapshot() != []

        await channel.update_graph(Graph(nodes=[node]))  # simulate a refresh

        reset_message = await _collect_until(subscriber, lambda m: isinstance(m, RuntimeResetMessage))
        assert isinstance(reset_message, RuntimeResetMessage)
        assert channel.state_store.snapshot() == []
    finally:
        channel.unsubscribe(subscriber)
        await channel.stop()


@pytest.mark.asyncio
async def test_snapshot_message_reflects_current_state() -> None:
    node = Node(id="mongo:db.tools:1", type="tool", name="foo", provenance=_prov())
    source = _ScriptedSource([_event(run_id="r1", phase="start")])

    channel = RuntimeChannel(batch_window_seconds=_FAST_BATCH_WINDOW)
    await channel.update_graph(Graph(nodes=[node]))
    await channel.set_source(source)
    subscriber = channel.subscribe()
    await channel.start()
    try:
        await _collect_until(subscriber, lambda m: isinstance(m, RuntimeDeltaMessage))

        snapshot = channel.snapshot_message()

        assert snapshot.type == "snapshot"
        assert len(snapshot.states) == 1
        assert snapshot.states[0].node_id == node.id
        assert snapshot.states[0].status == "active"
    finally:
        channel.unsubscribe(subscriber)
        await channel.stop()


@pytest.mark.asyncio
async def test_bridge_resolution_lights_up_function_and_tool_together() -> None:
    fn_node = Node(
        id="code:runtime_demo/tools.py:10:lookup_kb_tool",
        type="function",
        name="lookup_kb_tool",
        provenance=_prov(),
        attributes={"file_path": "runtime_demo/tools.py", "qualname": "lookup_kb_tool"},
    )
    tool_node = Node(id="mongo:db.tools:1", type="tool", name="lookup_kb", provenance=_prov())
    edge = Edge(source_id=tool_node.id, target_id=fn_node.id, kind="implements", provenance=_prov())

    fn_event = TraceEvent(
        v=1, ts=_TS, run_id="r1", span_id="s1", phase="start", kind="function",
        ref=TraceRef(name="lookup_kb_tool", module="runtime_demo.tools", qualname="lookup_kb_tool"),
    )
    source = _ScriptedSource([fn_event])

    channel = RuntimeChannel(batch_window_seconds=_FAST_BATCH_WINDOW)
    await channel.update_graph(Graph(nodes=[fn_node, tool_node], edges=[edge]))
    await channel.set_source(source)
    await channel.start()
    try:
        for _ in range(50):
            states = {s.node_id: s.status for s in channel.state_store.snapshot()}
            if len(states) == 2:
                break
            await asyncio.sleep(0.02)
        assert states == {fn_node.id: "active", tool_node.id: "active"}
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    channel = RuntimeChannel(batch_window_seconds=_FAST_BATCH_WINDOW)
    await channel.stop()  # never started
    await channel.start()
    await channel.stop()
    await channel.stop()  # already stopped
