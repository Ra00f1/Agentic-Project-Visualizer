"""Scanner Protocol + ScanContext.

Same Protocol-over-ABC decision as Connector — see `connectors/base.py` for
the rationale. In short: no forced inheritance, plays well with mypy,
third-party scanners don't need to import us.

A scan is *purely functional* over its inputs: given the same context
(connector state + clock + mapping), a scanner MUST produce the same
`ScanResult`. That determinism is what makes the merge/refresh story sane —
if a scanner's output drifts between runs on unchanged sources, diffs become
meaningless. Concrete implementations that need randomness or wall-clock time
should get it via the injected `now` on `ScanContext`, not from `time.time()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol, runtime_checkable

from ..connectors.base import Connector
from ..domain import ScanResult


@dataclass(slots=True, frozen=True)
class ScanContext:
    """Everything a scanner needs to run.

    Kept as a `@dataclass` rather than a `pydantic.BaseModel` because it
    carries a live connector and a callable — neither of which is a natural
    Pydantic field. Dataclass gives us the same immutability guarantee
    (`frozen=True`) without wrestling with `arbitrary_types_allowed`.

    The `now` field is deliberately a callable, not a `datetime`. Reason:
    tests inject a fixed clock, so provenance timestamps are deterministic
    and assertable. Prod injects `lambda: datetime.now(tz=timezone.utc)`.
    """

    connector: Connector
    now: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc)  # noqa: E731


@runtime_checkable
class Scanner(Protocol):
    """One stage of the introspection pipeline.

    A Scanner may raise for infrastructure failures (DB down, disk unreadable)
    — those bubble to the endpoint as 5xx. For per-item problems (missing
    field, unresolved reference), the scanner appends a `ScanError` to the
    result and keeps going. See `ScanError` docstring for the reasoning.
    """

    name: str
    """Short, stable identifier used in provenance. e.g. 'l1_entity'."""

    async def scan(self, context: ScanContext) -> ScanResult:
        """Run the scan and return its result."""
        ...
