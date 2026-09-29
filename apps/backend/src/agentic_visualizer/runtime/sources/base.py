"""TraceSource Protocol -- the seam RuntimeManager (and everything downstream) consumes.

Same Protocol-over-ABC decision as `connectors.base.Connector` and
`scanners.base.Scanner`: structural typing, no forced inheritance, a bad
implementation fails mypy rather than needing `@abstractmethod` bookkeeping.

Only one implementation ships in this task (`FileTailTraceSource`). The
Protocol exists now because two more are already planned: an SDK-over-socket
source and a stdin source for spawn-and-capture use cases. Both will plug in
here without `RuntimeManager` or the correlation layer (task NN+2) changing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from agentic_visualizer.trace.events import TraceEvent


@runtime_checkable
class TraceSource(Protocol):
    """A live stream of `TraceEvent`s from some origin.

    Contract notes:

    * `start()` must be called before `events()` is iterated. Implementations
      may raise if `events()` is consumed first.
    * `stop()` must be idempotent -- calling it twice, or calling it before
      `start()`, must not raise.
    * `events()` yields events in receipt order for as long as the source is
      running. It does not raise on a transient upstream hiccup (a malformed
      line, a slow writer) -- those are the source's own concern to absorb or
      count, not to surface as an iteration failure.
    """

    async def start(self) -> None:
        """Begin producing events. Safe to iterate `events()` immediately after."""
        ...

    async def stop(self) -> None:
        """Stop producing events and release any resources. Idempotent."""
        ...

    def events(self) -> AsyncIterator[TraceEvent]:
        """An async iterator of events, in receipt order."""
        ...
