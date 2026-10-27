"""TraceSource Protocol -- the seam RuntimeChannel (and everything downstream) consumes.

Same Protocol-over-ABC decision as `connectors.base.Connector` and
`scanners.base.Scanner`: structural typing, no forced inheritance, a bad
implementation fails mypy rather than needing `@abstractmethod` bookkeeping.

Only one implementation ships today (`FileTailTraceSource`, wrapped by the
`apv_live_file` `TraceMethod` -- see `runtime/methods/`). The Protocol
exists because more are already planned (Task 24's OTLP/HTTP source, and
others after it); `RuntimeChannel` runs however many are configured at
once, each behind this same seam.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agentic_visualizer.trace.events import TraceEvent

if TYPE_CHECKING:
    from agentic_visualizer.runtime.messages import SourceStatus


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
    * `status()` is read at any time, even before `start()`. It must not
      raise and must not block.
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

    def status(self) -> SourceStatus:
        """This source's own view of its health (Task 22) -- `state` and
        `detail` only; `method_id`/`label`/`events_received`/`events_ignored`
        are channel-level concerns a source has no way to know about, so
        implementations return placeholder values for those four
        (`RuntimeChannel.source_statuses()` always overwrites them via
        `model_copy`). A `str | None` return for `detail` keeps this honest
        about what one source genuinely knows -- e.g. a file path, or an
        error message -- without inventing meaning for fields it doesn't
        own.
        """
        ...
