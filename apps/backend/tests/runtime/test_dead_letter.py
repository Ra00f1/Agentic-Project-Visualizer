"""Unit tests for DeadLetterBuffer."""

from __future__ import annotations

from datetime import UTC, datetime

from agentic_visualizer.runtime.dead_letter import DeadLetterBuffer
from agentic_visualizer.trace.events import TraceEvent, TraceRef

_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _event(name: str = "foo") -> TraceEvent:
    return TraceEvent(
        v=1,
        ts=_TS,
        run_id="run-1",
        span_id="span-1",
        phase="start",
        kind="tool",
        ref=TraceRef(name=name, module="pkg.mod", qualname="foo_fn"),
    )


def test_record_and_snapshot() -> None:
    buffer = DeadLetterBuffer()
    buffer.record(_event("foo"), "no node matches")

    entries = buffer.snapshot()

    assert len(entries) == 1
    assert entries[0].ref_name == "foo"
    assert entries[0].ref_module == "pkg.mod"
    assert entries[0].ref_qualname == "foo_fn"
    assert entries[0].reason == "no node matches"


def test_stats_reports_current_count() -> None:
    buffer = DeadLetterBuffer()
    assert buffer.stats().count == 0

    buffer.record(_event("a"), "reason a")
    buffer.record(_event("b"), "reason b")

    assert buffer.stats().count == 2


def test_bounded_at_max_entries_evicts_oldest() -> None:
    buffer = DeadLetterBuffer(max_entries=3)
    for i in range(5):
        buffer.record(_event(f"item-{i}"), "reason")

    entries = buffer.snapshot()

    assert len(entries) == 3
    assert [e.ref_name for e in entries] == ["item-2", "item-3", "item-4"]
