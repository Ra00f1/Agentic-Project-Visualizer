"""End-to-end tests for RuntimeChannel: TraceSource -> resolve -> apply -> batch -> publish.

Uses a fake TraceSource (`tests.conftest.ScriptedTraceSource`) attached
through a fake `TraceMethod` (`tests.conftest.FakeTraceMethod`) rather than
a real `FileTailTraceSource` or the real `TRACE_METHODS` registry -- this
file cares about the correlation/state/batching/publish/per-source-status
pipeline, not file IO or the real method catalog (that's `test_methods.py`'s
job, Task 22).
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from agentic_visualizer.domain import Edge, Graph, Node, Provenance
from agentic_visualizer.runtime.channel import MethodConfig, RuntimeChannel
from agentic_visualizer.runtime.messages import (
    DeadLetterStatsMessage,
    RuntimeDeltaMessage,
    RuntimeMessage,
    RuntimeResetMessage,
)
from agentic_visualizer.runtime.methods.base import TraceMethod
from agentic_visualizer.runtime.methods.otlp_http import OtlpHttpMethod, OtlpHttpSettings
from agentic_visualizer.runtime.sources.base import TraceSource
from agentic_visualizer.runtime.sources.file_tail import FileTailTraceSource
from agentic_visualizer.trace.events import TraceEvent, TraceRef
from tests.conftest import FakeTraceMethod, FakeTraceMethodSettings
from tests.conftest import ScriptedTraceSource as _ScriptedSource

_TS = datetime(2026, 1, 1, tzinfo=UTC)
_FAST_BATCH_WINDOW = 0.01


async def _attach(channel: RuntimeChannel, method: TraceMethod) -> None:
    """Convenience for tests that only ever attach ONE source and don't
    care about `apply_config`'s own return value."""
    await channel.apply_config([MethodConfig(id=method.id, settings=FakeTraceMethodSettings())])


def _channel_with_source(source: TraceSource, **kwargs: object) -> tuple[RuntimeChannel, TraceMethod]:
    """A channel pre-wired with a single fake method wrapping `source`,
    ready for `_attach`. `**kwargs` forwards to `RuntimeChannel.__init__`
    (e.g. `batch_window_seconds`)."""
    method = FakeTraceMethod(source)
    channel = RuntimeChannel(methods={method.id: method}, **kwargs)  # type: ignore[arg-type]
    return channel, method


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
    channel, method = _channel_with_source(source, batch_window_seconds=_FAST_BATCH_WINDOW)

    await channel.update_graph(Graph(nodes=[node]))
    await _attach(channel, method)
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
    channel, method = _channel_with_source(source, batch_window_seconds=_FAST_BATCH_WINDOW)

    await channel.update_graph(Graph())  # empty graph -- nothing can resolve
    await _attach(channel, method)
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
    channel, method = _channel_with_source(source, batch_window_seconds=_FAST_BATCH_WINDOW)

    await channel.update_graph(Graph(nodes=[node]))
    await _attach(channel, method)
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
    channel, method = _channel_with_source(source, batch_window_seconds=_FAST_BATCH_WINDOW)

    await channel.update_graph(Graph(nodes=[node]))
    await _attach(channel, method)
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
    channel, method = _channel_with_source(source, batch_window_seconds=_FAST_BATCH_WINDOW)

    await channel.update_graph(Graph(nodes=[fn_node, tool_node], edges=[edge]))
    await _attach(channel, method)
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
async def test_orphan_sweep_evicts_a_stuck_span_and_publishes_idle() -> None:
    """Task 9 Scope D, end to end: a span with no matching end/error must not
    keep a node `active` forever. Both the TTL and the sweep interval are
    tiny constructor kwargs here so the test doesn't wait 300s/30s for real."""
    node = Node(id="mongo:db.tools:1", type="tool", name="foo", provenance=_prov())
    source = _ScriptedSource([_event(run_id="r1", phase="start")])
    channel, method = _channel_with_source(
        source,
        batch_window_seconds=_FAST_BATCH_WINDOW,
        orphaned_span_ttl_seconds=0.05,
        orphan_sweep_interval_seconds=0.05,
    )

    await channel.update_graph(Graph(nodes=[node]))
    await _attach(channel, method)
    subscriber = channel.subscribe()
    await channel.start()
    try:
        active_delta = await _collect_until(subscriber, lambda m: isinstance(m, RuntimeDeltaMessage))
        assert isinstance(active_delta, RuntimeDeltaMessage)
        assert active_delta.status == "active"

        idle_delta = await _collect_until(
            subscriber,
            lambda m: isinstance(m, RuntimeDeltaMessage) and m.status == "idle",
            timeout=2.0,
        )
        assert isinstance(idle_delta, RuntimeDeltaMessage)
        assert idle_delta.node_id == node.id
    finally:
        channel.unsubscribe(subscriber)
        await channel.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    channel = RuntimeChannel(batch_window_seconds=_FAST_BATCH_WINDOW)
    await channel.stop()  # never started
    await channel.start()
    await channel.stop()
    await channel.stop()  # already stopped


@pytest.mark.asyncio
async def test_dead_letter_stats_message_reports_event_counters_per_source() -> None:
    """Task 21 Fix 3 + Task 22: `eventsResolved` is a cross-source total;
    each source's own received/ignored counts live on its `SourceStatus`
    entry in `sources`, not as top-level fields."""
    node = Node(id="mongo:db.tools:1", type="tool", name="foo", provenance=_prov())
    resolved_event = _event(run_id="r1", phase="start", name="foo")
    unresolved_event = _event(run_id="r2", phase="start", name="nothing_matches")
    source = _ScriptedSource([resolved_event, unresolved_event])
    channel, method = _channel_with_source(
        source, batch_window_seconds=_FAST_BATCH_WINDOW, dead_letter_stats_interval_seconds=0.02
    )

    await channel.update_graph(Graph(nodes=[node]))
    await _attach(channel, method)
    subscriber = channel.subscribe()
    await channel.start()
    try:
        stats = await _collect_until(
            subscriber, lambda m: isinstance(m, DeadLetterStatsMessage) and m.events_resolved >= 1
        )
        assert isinstance(stats, DeadLetterStatsMessage)
        assert stats.events_resolved == 1
        assert stats.count == 1
        assert len(stats.sources) == 1
        fake_status = stats.sources[0]
        assert fake_status.method_id == "fake"
        assert fake_status.state == "running"
        assert fake_status.events_received == 2
        assert fake_status.events_ignored == 1
    finally:
        channel.unsubscribe(subscriber)
        await channel.stop()


@pytest.mark.asyncio
async def test_dead_letter_stats_reports_the_active_file_tail_source(tmp_path: Path) -> None:
    """Task 21 Fix 3: a REAL `FileTailTraceSource` (not the scripted fake)
    behind `apv_live_file` still reports `state`/`detail` correctly once
    generalized through `source.status()` (Task 22)."""
    path = tmp_path / "trace.jsonl"
    path.write_bytes(b"")  # pre-existing -- the tailer reaches "running" immediately

    method = FakeTraceMethod(FileTailTraceSource(path), method_id="apv_live_file")
    channel = RuntimeChannel(
        methods={method.id: method},
        batch_window_seconds=_FAST_BATCH_WINDOW,
        dead_letter_stats_interval_seconds=0.02,
    )
    subscriber = channel.subscribe()
    await channel.start()
    await _attach(channel, method)
    try:
        stats = await _collect_until(
            subscriber,
            lambda m: isinstance(m, DeadLetterStatsMessage)
            and any(s.state == "running" for s in m.sources),
        )
        assert isinstance(stats, DeadLetterStatsMessage)
        assert len(stats.sources) == 1
        assert stats.sources[0].detail == str(path)
    finally:
        channel.unsubscribe(subscriber)
        await channel.stop()


@pytest.mark.asyncio
async def test_replacing_a_source_stops_the_old_one() -> None:
    """Moved from Task 2's `tests/runtime/test_manager.py` (deleted, Task
    22 -- `RuntimeManager` is folded into `RuntimeChannel`): re-applying
    the same method id with DIFFERENT settings stops the previous source
    and starts a new one in its place -- events only ever come from
    whichever source is currently active."""
    first = _ScriptedSource([_event(run_id="r1", phase="start", name="first_event")])
    second = _ScriptedSource([_event(run_id="r2", phase="start", name="second_event")])
    method = FakeTraceMethod(first)
    channel = RuntimeChannel(methods={method.id: method}, batch_window_seconds=_FAST_BATCH_WINDOW)
    await channel.start()
    try:
        await channel.apply_config([MethodConfig(id=method.id, settings=FakeTraceMethodSettings(tag="a"))])
        # No graph loaded -- `first`'s event is unresolved, so its arrival
        # (and full processing) is observable via the dead-letter count.
        for _ in range(50):
            if channel.dead_letter.stats().count >= 1:
                break
            await asyncio.sleep(0.02)
        assert channel.dead_letter.stats().count == 1

        method._source = second  # the SAME method id will now create `second` instead
        await channel.apply_config([MethodConfig(id=method.id, settings=FakeTraceMethodSettings(tag="b"))])
        assert first.stopped

        for _ in range(50):
            if channel.dead_letter.stats().count >= 2:
                break
            await asyncio.sleep(0.02)
        assert channel.dead_letter.stats().count == 2  # first's event (1) + second's event (1)
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_otlp_source_pre_filtered_ignores_are_added_to_source_status() -> None:
    """Review finding: `OtlpHttpTraceSource.events_ignored` (spans filtered
    by `span_mapping.classify` BEFORE ever reaching `_process_event`) must
    still surface in `SourceStatus.events_ignored` -- the channel-tracked
    counter alone stays 0 here since nothing ever reaches `NodeIndex.
    resolve()` for a pre-filtered span."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    channel = RuntimeChannel(methods={"otlp_http": OtlpHttpMethod()}, batch_window_seconds=_FAST_BATCH_WINDOW)
    await channel.update_graph(Graph())
    await channel.apply_config([MethodConfig(id="otlp_http", settings=OtlpHttpSettings(port=port))])
    await channel.start()
    try:
        request = ExportTraceServiceRequest()
        resource_spans = request.resource_spans.add()
        scope_spans = resource_spans.scope_spans.add()
        span = scope_spans.spans.add()
        span.span_id = b"\x01" * 8
        span.name = "GET /healthz"  # no apv.*/openinference/gen_ai/code attrs -- unrecognized
        span.attributes.append(KeyValue(key="http.method", value=AnyValue(string_value="GET")))

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/v1/traces",
                content=request.SerializeToString(),
                headers={"content-type": "application/x-protobuf"},
            )
        assert resp.status_code == 200

        for _ in range(50):
            statuses = await channel.source_statuses()
            if statuses and statuses[0].events_ignored >= 1:
                break
            await asyncio.sleep(0.02)

        assert statuses[0].events_ignored == 1
        assert statuses[0].events_received == 0  # the span never reached _process_event at all
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_start_and_end_in_one_batch_window_coalesce_into_one_delta() -> None:
    """Task 23, Verification item 8: a start then end for the SAME node,
    both landing inside one `batch_window_seconds`, publish as ONE delta
    (`_pending` is keyed by node_id -- the end overwrites the start's
    entry) carrying the run_count bump from the start AND the
    `last_ended_at` the end set."""
    node = Node(id="mongo:db.tools:1", type="tool", name="foo", provenance=_prov())
    source = _ScriptedSource(
        [_event(run_id="r1", phase="start"), _event(run_id="r1", phase="end")]
    )
    channel, method = _channel_with_source(source, batch_window_seconds=_FAST_BATCH_WINDOW)

    await channel.update_graph(Graph(nodes=[node]))
    await _attach(channel, method)
    subscriber = channel.subscribe()
    await channel.start()
    try:
        delta = await _collect_until(
            subscriber, lambda m: isinstance(m, RuntimeDeltaMessage) and m.last_ended_at is not None
        )
        assert isinstance(delta, RuntimeDeltaMessage)
        assert delta.node_id == node.id
        assert delta.status == "idle"  # the end also closed the only active span
        assert delta.run_count == 1
        assert delta.last_ended_at is not None
    finally:
        channel.unsubscribe(subscriber)
        await channel.stop()


@pytest.mark.asyncio
async def test_duplicate_span_across_two_sources_counts_once() -> None:
    """Task 23 de-dup: the exact same (span_id, phase) pair delivered by
    two DIFFERENT sources (simulating a target that runs apv_trace AND
    exports OTLP at once) must not double-count -- run_count/error_count
    reflect one real invocation, not two."""
    node = Node(id="mongo:db.tools:1", type="tool", name="foo", provenance=_prov())
    start = _event(run_id="shared-run", phase="start")
    end_with_error = TraceEvent(
        v=1,
        ts=_TS,
        run_id="shared-run",
        span_id=start.span_id,  # same span_id as `start` -- the dedup key
        phase="error",
        kind="tool",
        ref=TraceRef(name="foo", module="pkg.mod"),
        meta={"error": {"type": "RuntimeError", "message": "boom", "traceback": ""}},
    )
    source_a = _ScriptedSource([start, end_with_error])
    source_b = _ScriptedSource([start, end_with_error])  # the SAME span, from a second source
    method_a = FakeTraceMethod(source_a, method_id="method_a")
    method_b = FakeTraceMethod(source_b, method_id="method_b")
    channel = RuntimeChannel(
        methods={method_a.id: method_a, method_b.id: method_b},
        batch_window_seconds=_FAST_BATCH_WINDOW,
    )

    await channel.update_graph(Graph(nodes=[node]))
    await channel.apply_config(
        [
            MethodConfig(id=method_a.id, settings=FakeTraceMethodSettings()),
            MethodConfig(id=method_b.id, settings=FakeTraceMethodSettings()),
        ]
    )
    subscriber = channel.subscribe()
    await channel.start()
    try:
        await _collect_until(
            subscriber, lambda m: isinstance(m, RuntimeDeltaMessage) and m.status == "errored"
        )
        # Give the second (duplicate) source's events a moment to be
        # processed and dropped -- there's no further publish to await on.
        await asyncio.sleep(_FAST_BATCH_WINDOW * 5)

        states = channel.state_store.snapshot()
        assert len(states) == 1
        assert states[0].run_count == 1
        assert states[0].error_count == 1
    finally:
        channel.unsubscribe(subscriber)
        await channel.stop()
