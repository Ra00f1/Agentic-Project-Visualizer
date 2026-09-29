"""Unit tests for RuntimeManager -- the thin facade over "whichever TraceSource is active"."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import pytest

from agentic_visualizer.runtime.manager import RuntimeManager
from agentic_visualizer.trace.events import TraceEvent, TraceRef

_TS = datetime.fromisoformat("2026-09-28T12:00:00.000000+00:00")


def _event(span_id: str) -> TraceEvent:
    return TraceEvent(
        ts=_TS,
        run_id="run-1",
        span_id=span_id,
        phase="start",
        kind="tool",
        ref=TraceRef(name="t", module="m"),
    )


class _FakeSource:
    """Minimal TraceSource: yields one canned event, tracks start/stop calls."""

    def __init__(self, event: TraceEvent) -> None:
        self._event = event
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def events(self) -> AsyncIterator[TraceEvent]:
        yield self._event


@pytest.mark.asyncio
async def test_events_raises_before_start() -> None:
    manager = RuntimeManager()
    with pytest.raises(RuntimeError):
        manager.events()


@pytest.mark.asyncio
async def test_start_then_events_yields_from_the_source() -> None:
    manager = RuntimeManager()
    source = _FakeSource(_event("a"))

    await manager.start(source)
    assert source.started

    events = [e async for e in manager.events()]
    assert [e.span_id for e in events] == ["a"]


@pytest.mark.asyncio
async def test_starting_a_new_source_stops_the_old_one() -> None:
    manager = RuntimeManager()
    first = _FakeSource(_event("a"))
    second = _FakeSource(_event("b"))

    await manager.start(first)
    await manager.start(second)

    assert first.stopped
    assert second.started
    events = [e async for e in manager.events()]
    assert [e.span_id for e in events] == ["b"]


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_clears_the_active_source() -> None:
    manager = RuntimeManager()
    source = _FakeSource(_event("a"))

    await manager.stop()  # nothing active yet -- must not raise
    await manager.start(source)
    await manager.stop()
    assert source.stopped
    with pytest.raises(RuntimeError):
        manager.events()

    await manager.stop()  # already stopped -- must not raise
