"""RuntimeManager -- owns the currently-active TraceSource for the backend.

A thin façade so the rest of the app (the WebSocket layer, task NN+2) talks
to "whatever trace source is active right now" without knowing whether
that's a file tailer, a future SDK-over-socket source, or a stdin capture.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from agentic_visualizer.runtime.sources.base import TraceSource
from agentic_visualizer.trace.events import TraceEvent


class RuntimeManager:
    """Owns at most one active `TraceSource` at a time."""

    def __init__(self) -> None:
        self._source: TraceSource | None = None

    async def start(self, source: TraceSource) -> None:
        """Start `source`, stopping and replacing any currently-active one."""
        if self._source is not None:
            await self._source.stop()
            # Clear before awaiting the new source's start(): if that raises
            # (a real risk for a future socket/subprocess-backed source,
            # unlike the current polling-based file tailer), we want
            # events() to fail loudly with "no active source" rather than
            # silently keep serving from the now-stopped old one.
            self._source = None
        await source.start()
        self._source = source

    async def stop(self) -> None:
        """Idempotent: a no-op if nothing is active."""
        if self._source is None:
            return
        await self._source.stop()
        self._source = None

    def events(self) -> AsyncIterator[TraceEvent]:
        """The active source's event stream.

        Raises `RuntimeError` if nothing has been started -- there's no
        sensible "empty" iterator to hand back that wouldn't silently mask a
        caller forgetting to call `start()` first.
        """
        if self._source is None:
            raise RuntimeError("RuntimeManager has no active source -- call start() first")
        return self._source.events()
