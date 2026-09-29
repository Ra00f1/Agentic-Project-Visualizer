"""FileTailTraceSource -- tails a JSONL trace log a target project writes to.

The classic "tail -F" problem in async Python. Structured as two coroutines
coordinated by a bounded queue:

    [reader task] --parses lines into TraceEvents--> asyncio.Queue --> events()

The reader task owns the file handle and all mutable state (buffer, inode,
counters); `events()` is a thin async-generator consumer of the queue, so
multiple things could iterate it without touching the reader's internals
(not a supported use case today, but the split costs nothing).

Backpressure: drop oldest, not newest. If the target project outruns us, the
most recent runtime state (what's happening *now*) is more useful than the
oldest queued events, and dropping from the head is more likely to drop a
matched start/end pair together than to orphan one half of it.

Resilience: once the reader task is past its initial open, any I/O error
(read failure, a failed reopen during rotation) is caught, counted, and
retried rather than left to kill the task -- see `TraceSource`'s Protocol
docstring: "does not raise on a transient upstream hiccup". A permanent
failure just means the error counter climbs and no more events arrive,
which is visible via `stats()`, rather than the source silently going dead
with no signal at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import aiofiles
from pydantic import ValidationError

from agentic_visualizer.trace.events import TraceEvent

logger = logging.getLogger("agentic_visualizer")

_POLL_INTERVAL_SECONDS = 0.05
_MISSING_FILE_POLL_SECONDS = 0.2
_READ_CHUNK_BYTES = 65536


@dataclass(frozen=True, slots=True)
class FileTailStats:
    """Health counters for a running `FileTailTraceSource`."""

    dropped_count: int
    """Events evicted from the bounded queue because a consumer fell behind."""

    parse_error_count: int
    """Lines that failed `TraceEvent.model_validate_json` and were skipped."""

    io_error_count: int
    """Transient read/reopen failures (disk hiccup, a share-violation during
    rotation) that were caught and retried rather than killing the reader."""


async def _stat_ino_and_size(path: Path) -> tuple[int, int] | None:
    """`(inode, size)` of `path`, or None if missing. One `stat()` call, two uses.

    Inode (Windows: NTFS file index, emulated by Python) comparison across
    polls is how rotation is detected: a rename-away + recreate at the same
    path produces a different inode even though the path string is
    unchanged. Size comparison against the reader's own position is how
    in-place truncation is detected (see `_read_loop`) -- a single `stat()`
    already carries both, so there's no reason to call it twice per poll
    tick for two separate checks. Offloaded via `asyncio.to_thread` (CLAUDE.md
    §5: "async on all IO") -- matters most on a slow/network-backed path or
    when Windows Defender's real-time scanning intercepts the stat.
    """
    try:
        stat = await asyncio.to_thread(path.stat)
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_size


class FileTailTraceSource:
    """Tails `path`, emitting parsed `TraceEvent`s as the target project appends to it."""

    def __init__(self, path: Path, *, from_beginning: bool = False, max_queue: int = 1000) -> None:
        self._path = path
        self._from_beginning = from_beginning
        self._queue: asyncio.Queue[TraceEvent] = asyncio.Queue(maxsize=max_queue)
        self._reader_task: asyncio.Task[None] | None = None
        # Signals "stopped" to any consumer parked inside events() -- see
        # that method. The reader task itself stops via task.cancel(), not
        # this event; the two mechanisms serve different consumers.
        self._stop_event = asyncio.Event()
        self._dropped_count = 0
        self._parse_error_count = 0
        self._io_error_count = 0

    def stats(self) -> FileTailStats:
        """Snapshot of the health counters. Safe to call from any task."""
        return FileTailStats(
            dropped_count=self._dropped_count,
            parse_error_count=self._parse_error_count,
            io_error_count=self._io_error_count,
        )

    async def start(self) -> None:
        if self._reader_task is not None:
            raise RuntimeError("FileTailTraceSource is already started")
        self._stop_event.clear()
        self._reader_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        """Idempotent: calling this before `start()` or more than once is a no-op.

        Clears `self._reader_task` *before* awaiting it, not after -- if the
        reader died from something the loop's own OSError handling didn't
        catch (e.g. the very first open failing), awaiting it re-raises that
        exception, and clearing state first means the instance is still left
        cleanly stopped and restartable rather than permanently wedged with
        `start()` forever refusing ("already started") a dead task.
        """
        if self._reader_task is None:
            return
        self._stop_event.set()
        task = self._reader_task
        self._reader_task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - reader's own failure must not wedge stop()
            logger.debug(
                "reader task for %s ended with %s: %s", self._path, exc.__class__.__name__, exc
            )
        # Drain so a consumer that keeps iterating events() after stop()
        # doesn't replay whatever was still queued.
        while not self._queue.empty():
            self._queue.get_nowait()

    async def events(self) -> AsyncIterator[TraceEvent]:
        """Yield queued events until `stop()` is called, then end cleanly.

        Races `queue.get()` against `_stop_event` rather than just awaiting
        the queue directly: without this, a consumer parked here when
        `stop()` runs would hang forever -- `stop()` drains whatever was
        already queued but nothing else would ever wake this `get()`.
        """
        stop_waiter = asyncio.ensure_future(self._stop_event.wait())
        get_waiter: asyncio.Task[TraceEvent] | None = None
        try:
            while True:
                get_waiter = asyncio.ensure_future(self._queue.get())
                done, _pending = await asyncio.wait(
                    {get_waiter, stop_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                if get_waiter in done:
                    result = get_waiter.result()
                    get_waiter = None
                    yield result
                    continue
                get_waiter.cancel()
                get_waiter = None
                return
        finally:
            # If this generator is torn down by an external cancellation
            # (e.g. asyncio.wait_for's timeout on __anext__()) rather than
            # through the branches above, `get_waiter` can still be a live,
            # un-cancelled asyncio.Queue.get() call. Left alive, it would
            # keep competing for the NEXT item some other consumer puts on
            # the queue -- Queue wakes waiters FIFO, so an orphaned get()
            # can silently steal an event meant for a fresh events() call.
            stop_waiter.cancel()
            if get_waiter is not None:
                get_waiter.cancel()

    async def _read_loop(self) -> None:
        handle = await self._open_when_available()
        stat_now = await _stat_ino_and_size(self._path)
        inode = stat_now[0] if stat_now is not None else None
        buffer = b""
        try:
            while True:
                try:
                    chunk = await handle.read(_READ_CHUNK_BYTES)
                    if chunk:
                        buffer += chunk
                        *complete_lines, buffer = buffer.split(b"\n")
                        for line in complete_lines:
                            self._handle_line(line)
                        continue

                    # An empty read doesn't by itself mean "caught up to the
                    # writer" -- the file's identity or length may have
                    # changed under us since the last check, and this handle
                    # won't notice on its own:
                    #   * rotation (rename-away + recreate at the same path)
                    #     changes the inode but not necessarily the size.
                    #   * in-place truncation (a `copytruncate`-style rotator,
                    #     or a test harness resetting a fixture log) keeps the
                    #     inode but shortens the file below this handle's own
                    #     read position -- and on Windows a buffered handle
                    #     left there can get stuck returning empty reads
                    #     forever, even once the writer appends past the new
                    #     EOF, because nothing forces it to re-sync with the
                    #     file's actual current size.
                    # Either case needs the same recovery -- reopen -- so one
                    # `stat()` and one condition cover both instead of two
                    # separate checks that would otherwise each need their
                    # own syscall and their own copy of the reopen logic.
                    new_stat = await _stat_ino_and_size(self._path)
                    if new_stat is not None:
                        new_inode, new_size = new_stat
                        truncated_in_place = new_inode == inode and new_size < await handle.tell()
                        if new_inode != inode or truncated_in_place:
                            logger.debug(
                                "%s changed identity or was truncated in place "
                                "(inode %r -> %r, size %d); reopening",
                                self._path,
                                inode,
                                new_inode,
                                new_size,
                            )
                            await handle.close()
                            handle = await aiofiles.open(self._path, "rb")
                            inode = new_inode
                            buffer = b""
                            continue

                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                except OSError as exc:
                    # Read failure or a reopen that lost a race (e.g. the
                    # rotator briefly held an exclusive lock). Count it and
                    # keep going -- see module docstring on resilience.
                    self._io_error_count += 1
                    logger.debug("transient I/O error tailing %s: %s", self._path, exc)
                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        finally:
            with contextlib.suppress(OSError):
                await handle.close()

    async def _open_when_available(self) -> aiofiles.threadpool.binary.AsyncBufferedIOBase:
        while not await asyncio.to_thread(self._path.exists):
            await asyncio.sleep(_MISSING_FILE_POLL_SECONDS)
        handle = await aiofiles.open(self._path, "rb")
        if not self._from_beginning:
            await handle.seek(0, 2)  # SEEK_END -- only new events from here on.
        return handle

    def _handle_line(self, raw_line: bytes) -> None:
        stripped = raw_line.strip()
        if not stripped:
            return
        try:
            event = TraceEvent.model_validate_json(stripped)
        except ValidationError as exc:
            self._parse_error_count += 1
            logger.debug("skipping malformed trace line: %s", exc)
            return
        self._put_dropping_oldest(event)

    def _put_dropping_oldest(self, event: TraceEvent) -> None:
        # Single-producer queue (only _read_loop ever puts): no other task
        # can race to fill a slot between the full() check and this put.
        if self._queue.full():
            self._queue.get_nowait()
            self._dropped_count += 1
        self._queue.put_nowait(event)
