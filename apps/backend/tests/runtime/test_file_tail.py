"""Unit tests for FileTailTraceSource.

Every scenario here maps 1:1 to a bullet in Task 2's Verification section --
see `.claude/Task 2.md`. Timing constants (`_POLL_INTERVAL_SECONDS` = 0.05s,
`_MISSING_FILE_POLL_SECONDS` = 0.2s in file_tail.py) mean these tests use
real sleeps and `asyncio.wait_for` timeouts rather than mocking the clock --
short enough to keep the suite fast, long enough to not be flaky.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agentic_visualizer.runtime.sources.file_tail import FileTailTraceSource
from agentic_visualizer.trace.events import TraceEvent


def _raw_event(span_id: str, name: str = "some_tool", *, phase: str = "start") -> dict[str, Any]:
    return {
        "v": 1,
        "ts": "2026-09-28T12:00:00.000000+00:00",
        "run_id": "run-1",
        "span_id": span_id,
        "parent_span_id": None,
        "phase": phase,
        "kind": "tool",
        "ref": {
            "name": name,
            "module": "runtime_demo.tools",
            "qualname": f"{name}_tool",
            "framework_id": None,
            "db_id": None,
        },
        "meta": {},
    }


def _line(span_id: str, name: str = "some_tool") -> bytes:
    return _compact_json(_raw_event(span_id, name)) + b"\n"


def _compact_json(obj: Any) -> bytes:
    """No-whitespace JSON, matching apv_trace's real `orjson.dumps` wire
    format (`{"v":1,...`) -- the file-tailer's pre-filter (`_looks_like_trace_event`)
    specifically checks for `"v":1` with no space after the colon, so a test
    fixture built with stdlib `json.dumps`'s default (space-after-colon)
    separators would get silently rejected as non-trace noise before ever
    reaching the Pydantic parse this suite is testing."""
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


async def _next_event(gen: Any, timeout: float = 1.0) -> TraceEvent:
    return await asyncio.wait_for(gen.__anext__(), timeout=timeout)


async def _assert_no_event_yet(source: FileTailTraceSource, timeout: float = 0.15) -> None:
    # asyncio.wait_for's timeout cancels the pending anext() call. That
    # cancellation propagates through the generator's suspended
    # `await self._queue.get()` and closes it -- a *later* anext() on this
    # same generator object would raise StopAsyncIteration immediately. So
    # this helper uses its own short-lived generator; callers get a fresh
    # one via source.events() afterward for actual consumption.
    gen = source.events()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(gen.__anext__(), timeout=timeout)


@pytest.mark.asyncio
async def test_reads_existing_events_from_beginning(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_bytes(_line("a") + _line("b"))

    source = FileTailTraceSource(path, from_beginning=True)
    await source.start()
    try:
        gen = source.events()
        first = await _next_event(gen)
        second = await _next_event(gen)
        assert [first.span_id, second.span_id] == ["a", "b"]
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_reads_new_events_appended_after_start(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_bytes(b"")  # exists, but empty -- from_beginning doesn't matter here

    source = FileTailTraceSource(path)
    await source.start()
    try:
        await _assert_no_event_yet(source)

        with path.open("ab") as f:
            f.write(_line("appended"))

        gen = source.events()
        event = await _next_event(gen)
        assert event.span_id == "appended"
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_waits_for_not_yet_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    assert not path.exists()

    source = FileTailTraceSource(path, from_beginning=True)
    await source.start()
    try:
        await _assert_no_event_yet(source, timeout=0.3)

        path.write_bytes(_line("created-later"))

        gen = source.events()
        event = await _next_event(gen, timeout=1.5)
        assert event.span_id == "created-later"
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_a_burst_written_right_after_a_missing_file_appears_is_not_dropped(tmp_path: Path) -> None:
    """Task 21 Fix 1 -- the repro that found this bug: start tailing a path
    that doesn't exist yet (default `from_beginning=False`, matching the
    production call site at `main.py`'s `_lifespan`), then create the file
    and write a whole burst of lines in one shot, with no pause for the
    tailer's poll to catch up. Before the fix, `_open_when_available` always
    seeked to EOF once the file appeared -- which, by the time it ran, was
    already past this burst, so every line vanished (confirmed empirically:
    0 of 34 events received). All 34 must now arrive."""
    path = tmp_path / "trace.jsonl"
    assert not path.exists()

    source = FileTailTraceSource(path)  # from_beginning NOT set -- the real default
    await source.start()
    try:
        await asyncio.sleep(0.3)  # let the tailer start waiting for the file
        state_before = source.state
        assert state_before == "waiting_for_file"

        path.write_bytes(b"".join(_line(str(i)) for i in range(34)))

        gen = source.events()
        received = [await _next_event(gen, timeout=1.0) for _ in range(34)]
        assert [e.span_id for e in received] == [str(i) for i in range(34)]
        state_after = source.state
        assert state_after == "tailing"
        assert source.path == path
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_state_is_tailing_immediately_for_a_pre_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_bytes(b"")

    source = FileTailTraceSource(path)
    await source.start()
    try:
        # No poll/sleep needed: the file already existed, so the reader
        # doesn't go through the waiting-for-file loop at all.
        for _ in range(20):
            if source.state == "tailing":
                break
            await asyncio.sleep(0.02)
        assert source.state == "tailing"
    finally:
        await source.stop()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows opens files without FILE_SHARE_DELETE by default (verified "
        "empirically -- see Task 2 Result): a plain aiofiles/stdlib open() "
        "handle blocks any rename of that path by another process/handle "
        "while we hold it, so a target project's own rotation would fail "
        "the same way this test's simulated rotation does. Detection logic "
        "itself (inode comparison + reopen) is exercised on POSIX."
    ),
)
@pytest.mark.asyncio
async def test_detects_rotation(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_bytes(_line("before-rotation"))

    source = FileTailTraceSource(path, from_beginning=True)
    await source.start()
    try:
        gen = source.events()
        first = await _next_event(gen)
        assert first.span_id == "before-rotation"

        # Rotation: rename the tailed file away, create a NEW file at the
        # same path -- a different inode, which is what triggers reopen.
        path.rename(tmp_path / "trace.jsonl.old")
        path.write_bytes(_line("after-rotation"))

        second = await _next_event(gen, timeout=1.5)
        assert second.span_id == "after-rotation"
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_recovers_from_in_place_truncation(tmp_path: Path) -> None:
    """Regression test for a real bug this file's own Playwright e2e suite

    (`apps/desktop/tests/e2e/runtime-overlay.spec.ts`) exposed: a test-harness
    truncate landing on the SAME path right as the reader opens it left the
    handle's read position past the file's new (shorter) end. Unlike the
    rename+recreate rotation `test_detects_rotation` covers, an in-place
    truncate (same inode -- this is what `Path.write_bytes` does, and what a
    `copytruncate`-style log rotator does too) doesn't trip the inode-change
    check, so without an explicit "position past current size" check the
    reader would sit forever getting empty reads, even once new content is
    appended past the truncation point.
    """
    path = tmp_path / "trace.jsonl"
    path.write_bytes(_line("before-truncate"))

    source = FileTailTraceSource(path, from_beginning=True)
    await source.start()
    try:
        gen = source.events()
        first = await _next_event(gen)
        assert first.span_id == "before-truncate"

        inode_before = path.stat().st_ino
        path.write_bytes(b"")  # in-place truncate -- same inode, shorter file
        assert path.stat().st_ino == inode_before
        # Give the reader a real chance to observe the truncated-to-zero
        # state (and reopen) BEFORE new content arrives -- otherwise this
        # test's outcome would depend on incidental line-length differences
        # between the two spans rather than actually exercising recovery.
        await asyncio.sleep(0.15)

        path.write_bytes(_line("after-truncate"))

        second = await _next_event(gen, timeout=1.5)
        assert second.span_id == "after-truncate"
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_drops_oldest_on_overflow(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    # 5 events into a 2-slot queue, written before start() so the reader
    # drains the whole file in one read() and processes all 5 synchronously
    # -- deterministic: evicts #1, #2, #3, keeping #4 and #5.
    path.write_bytes(b"".join(_line(str(i)) for i in range(5)))

    source = FileTailTraceSource(path, from_beginning=True, max_queue=2)
    await source.start()
    try:
        await asyncio.sleep(0.2)  # let the reader task drain the file
        stats = source.stats()
        assert stats.dropped_count == 3

        gen = source.events()
        remaining = [await _next_event(gen), await _next_event(gen)]
        assert [e.span_id for e in remaining] == ["3", "4"]
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_skips_malformed_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_bytes(b"not valid json at all\n" + _line("valid-one"))

    source = FileTailTraceSource(path, from_beginning=True)
    await source.start()
    try:
        gen = source.events()
        event = await _next_event(gen)
        assert event.span_id == "valid-one"
        assert source.stats().parse_error_count == 0  # pre-filter catches this one -- see below
        assert source.stats().non_trace_count == 1
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_non_trace_lines_are_counted_separately_from_parse_errors(tmp_path: Path) -> None:
    """Task 9 Scope A: a target project that mixes stdlib `logging`/`print()`
    output into the same file shouldn't inflate `parse_error_count` -- that
    counter is reserved for lines that LOOKED like our wire format (passed
    the cheap `{...\"v\":1...}` pre-filter) but failed schema validation,
    which is the actionable "something's wrong with MY emitter" signal.
    Plain log noise is normal and goes in `non_trace_count` instead."""
    path = tmp_path / "trace.jsonl"
    stdlib_log_line = b"2026-09-30 12:00:00 INFO myapp.worker: handling request\n"
    print_line = b"Starting up...\n"
    # Looks like our format (starts with `{`, has `"v":1` up front) but is
    # missing required fields -- THIS one must count as a parse error, not
    # non-trace noise.
    broken_trace_line = b'{"v":1,"not":"a valid trace event"}\n'

    path.write_bytes(stdlib_log_line + print_line + broken_trace_line + _line("valid-one"))

    source = FileTailTraceSource(path, from_beginning=True)
    await source.start()
    try:
        gen = source.events()
        event = await _next_event(gen)
        assert event.span_id == "valid-one"
        stats = source.stats()
        assert stats.non_trace_count == 2  # the stdlib log line + the print line
        assert stats.parse_error_count == 1  # the broken-but-trace-shaped line
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_handles_partial_trailing_line_across_two_writes(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    full_line = _line("split-across-writes")
    split_at = len(full_line) // 2
    path.write_bytes(full_line[:split_at])  # no trailing newline yet

    source = FileTailTraceSource(path, from_beginning=True)
    await source.start()
    try:
        await _assert_no_event_yet(source)

        with path.open("ab") as f:
            f.write(full_line[split_at:])

        gen = source.events()
        event = await _next_event(gen)
        assert event.span_id == "split-across-writes"
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_bytes(b"")
    source = FileTailTraceSource(path)

    await source.stop()  # never started -- must not raise
    await source.start()
    await source.stop()
    await source.stop()  # already stopped -- must not raise


@pytest.mark.asyncio
async def test_handles_crlf_line_endings(tmp_path: Path) -> None:
    """CLAUDE.md §11: test both line endings -- Windows-first project."""
    path = tmp_path / "trace.jsonl"
    line_without_terminator = _compact_json(_raw_event("crlf-test"))
    path.write_bytes(line_without_terminator + b"\r\n")

    source = FileTailTraceSource(path, from_beginning=True)
    await source.start()
    try:
        gen = source.events()
        event = await _next_event(gen)
        assert event.span_id == "crlf-test"
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_stop_unblocks_a_pending_consumer(tmp_path: Path) -> None:
    """A consumer parked in events() must not hang forever once stop() runs.

    Regression test: events() used to be a bare `while True: yield await
    self._queue.get()`, which nothing but a new item could ever wake --
    stop() draining the queue didn't touch an already-suspended get().
    """
    path = tmp_path / "trace.jsonl"
    path.write_bytes(b"")

    source = FileTailTraceSource(path)
    await source.start()
    gen = source.events()

    consumer = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.1)  # let the consumer actually park on the queue
    assert not consumer.done()

    await source.stop()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(consumer, timeout=1.0)


@pytest.mark.asyncio
async def test_stop_after_initial_open_failure_leaves_instance_restartable(tmp_path: Path) -> None:
    """A reader task that dies before its OSError-resilient loop starts must not wedge stop()/start().

    Opening a directory as if it were the trace file raises OSError
    (IsADirectoryError) from `_open_when_available()`, before `_read_loop`'s
    per-iteration try/except exists to catch it -- exactly the case that
    used to leave `stop()` re-raising and `_reader_task` never cleared.
    """
    not_a_file = tmp_path / "this_is_a_directory"
    not_a_file.mkdir()

    source = FileTailTraceSource(not_a_file)
    await source.start()
    await asyncio.sleep(0.1)  # let the reader task fail on the initial open

    await source.stop()  # must not raise, must not leave the instance wedged
    await source.start()  # must succeed -- proves the instance is restartable
    await source.stop()
