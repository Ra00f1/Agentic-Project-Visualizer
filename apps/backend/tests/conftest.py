"""Test fixtures.

Design decision: **integration tests hit a real, seeded MongoDB by default.**
The mock-agent-project fixture at the repo root exists exactly so we can do
this without brittle mock objects. The test suite auto-skips if Mongo isn't
reachable — that's the right failure mode for a machine without Mongo, but
the local dev loop is expected to have `mongod` (or Docker) running.

We DO NOT re-seed the DB from these tests. That's the seeder's job (see
`mock-agent-project/mongo/seed.py`). Tests are pure readers.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore[import-untyped]

from agentic_visualizer.scanners.base import ScanContext
from agentic_visualizer.trace.events import TraceEvent


# The env vars follow the same convention as the seeder script.
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "mock_agent")


@pytest.fixture(scope="session")
def fixed_clock():
    """Deterministic clock for scanner tests — makes provenance timestamps assertable."""
    return lambda: datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture(scope="session")
async def require_mongo():
    """Session-scoped guard: skip integration tests if Mongo isn't reachable.

    We probe with a short timeout so the test suite fails fast on machines
    without Mongo, rather than the default 30s ping timeout.
    """
    client: AsyncIOMotorClient = AsyncIOMotorClient(
        MONGO_URI, serverSelectionTimeoutMS=2_000
    )
    try:
        try:
            await client.admin.command("ping")
        except Exception as exc:  # noqa: BLE001 - deliberate broad catch for skip
            pytest.skip(f"MongoDB not reachable at {MONGO_URI} ({exc.__class__.__name__})")
        # Also check the fixture DB is actually seeded — if not, tests aren't
        # meaningful. Print a hint pointing at the seed command.
        db = client[MONGO_DB]
        names = await db.list_collection_names()
        expected = {"users", "models", "prompts", "tools", "agents", "workflows", "files"}
        missing = expected - set(names)
        if missing:
            pytest.skip(
                f"MongoDB db {MONGO_DB!r} is missing collections {sorted(missing)}. "
                f"Run: python mock-agent-project/mongo/seed.py --drop"
            )
    finally:
        client.close()
    return MONGO_URI, MONGO_DB


@pytest.fixture
def scan_context_factory(fixed_clock):
    """Return a factory that builds a ScanContext around a live connector.

    Used by the L1 and endpoint tests. Kept as a factory (not a fixture)
    because callers want to bind their own connector per test.
    """

    def _make(connector):
        return ScanContext(connector=connector, now=fixed_clock)

    return _make


# pytest-asyncio 1.x: `asyncio_mode = "auto"` in pyproject would work, but we
# opt to be explicit per-test via `@pytest.mark.asyncio` so it's obvious which
# tests need the event loop. No config needed for that here.


class ScriptedTraceSource:
    """Minimal TraceSource: yields a fixed list of events, then idles until stop().

    Matches `FileTailTraceSource`'s real behavior (`events()` never ends on
    its own while running) closely enough for runtime-channel/WS tests that
    only care about the correlation/state/publish pipeline, not file IO.
    Shared here (not duplicated per test file) since both
    `tests/runtime/test_channel.py` and `tests/api/test_runtime_ws.py` need
    the exact same double, and they're in different subpackages with no
    other common import point.
    """

    def __init__(self, events: list[TraceEvent]) -> None:
        self._events = events
        self._done = asyncio.Event()

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self._done.set()

    async def events(self) -> AsyncIterator[TraceEvent]:
        for event in self._events:
            yield event
        await self._done.wait()
