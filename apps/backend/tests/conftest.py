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
from typing import Any

import pytest
import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore[import-untyped]
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from agentic_visualizer.runtime.messages import SourceStatus
from agentic_visualizer.runtime.methods.base import TraceMethodInfo
from agentic_visualizer.runtime.sources.base import TraceSource
from agentic_visualizer.scanners.base import ScanContext
from agentic_visualizer.trace.events import TraceEvent

# The env vars follow the same convention as the seeder script.
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "mock_agent")

# Collection counts a correctly-seeded `mock_agent` DB must have -- the
# ground truth is `mock-agent-project/mongo/dump.json` + `seed.py` (see
# those files for the actual fixture content, not these numbers). Update
# this alongside dump.json/seed.py, together with the EXPECTED_* constants
# in test_l1_scanner.py/test_l2_scanner.py, which describe the SCANNED
# GRAPH these raw counts produce (see Task 10's Result for how they were
# derived and cross-checked).
_EXPECTED_COLLECTION_COUNTS = {
    "users": 10,
    "models": 8,
    "prompts": 28,
    "tools": 78,
    "agents": 133,
    "workflows": 22,
    "files": 1236,
}


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
        missing = set(_EXPECTED_COLLECTION_COUNTS) - set(names)
        if missing:
            pytest.skip(
                f"MongoDB db {MONGO_DB!r} is missing collections {sorted(missing)}. "
                f"Run: python mock-agent-project/mongo/seed.py --drop"
            )
    finally:
        client.close()
    return MONGO_URI, MONGO_DB


@pytest.fixture(autouse=True, scope="session")
def _mongo_fixture_integrity_check() -> None:
    """Session-start guard: `pytest.exit` loudly if `mock_agent`'s collection
    counts don't match what a correct seed produces -- stops the kind of
    silent drift Task 10 reconciled (the L1/L2 EXPECTED_* constants going
    stale against a fixture that grew without anyone updating them) from
    recurring unnoticed.

    Deliberately NOT built on `require_mongo` and does NOT call
    `pytest.skip`: this must stay a silent no-op on a machine with no Mongo
    at all (or an unseeded one), so test files that don't touch Mongo
    (test_domain.py, trace/test_events.py, ...) keep working exactly as
    this conftest's own module docstring promises -- `require_mongo`
    already reports "not reachable" / "not seeded" per-test, for the tests
    that actually need it. This fixture only ever acts when Mongo IS
    reachable AND the DB IS seeded AND a count is actually wrong -- a
    single failing count assertion buried in 130+ test results is easy to
    miss; a session-start abort listing every mismatched collection is not.

    One try/except around the whole body (not just the initial ping) so a
    transient disconnect between the ping and the count_documents() calls
    is also a silent no-op rather than an uncaught traceback aborting the
    session -- and one try/finally so the client is closed on every exit
    path, including the "unreachable" one (a bare `except: return` right
    after opening the client would leak it).
    """
    drifted: dict[str, tuple[int, int]] = {}
    try:
        client: MongoClient[dict[str, Any]] = MongoClient(MONGO_URI, serverSelectionTimeoutMS=1_000)
        try:
            client.admin.command("ping")
            db = client[MONGO_DB]
            if not set(_EXPECTED_COLLECTION_COUNTS) <= set(db.list_collection_names()):
                return  # not seeded -- require_mongo reports this per-test instead
            drifted = {
                coll: (db[coll].count_documents({}), expected)
                for coll, expected in _EXPECTED_COLLECTION_COUNTS.items()
            }
            drifted = {coll: pair for coll, pair in drifted.items() if pair[0] != pair[1]}
        finally:
            client.close()
    except PyMongoError:
        return  # not reachable (or died mid-check) -- not this fixture's job to report
    if drifted:
        lines = "\n".join(
            f"  {coll}: expected {expected}, got {actual}"
            for coll, (actual, expected) in drifted.items()
        )
        pytest.exit(
            f"mock_agent has drifted from the documented fixture counts:\n{lines}\n"
            f"Reseed: python mock-agent-project/mongo/seed.py --drop\n"
            f"(then update EXPECTED_* in test_l1_scanner.py/test_l2_scanner.py "
            f"if the drift was an intentional fixture change)",
            returncode=2,
        )


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

    @property
    def stopped(self) -> bool:
        return self._done.is_set()

    async def events(self) -> AsyncIterator[TraceEvent]:
        for event in self._events:
            yield event
        await self._done.wait()

    def status(self) -> SourceStatus:
        # `method_id`/`label`/`events_received`/`events_ignored` are always
        # overwritten by `RuntimeChannel.source_statuses()` -- see
        # `TraceSource.status()`'s own docstring. `"running"` the instant
        # this is constructed: unlike `FileTailTraceSource`, there's no
        # "waiting for something to exist" phase to model here.
        return SourceStatus(
            method_id="", label="", state="running", detail=None, events_received=0, events_ignored=0
        )


class FakeTraceMethodSettings(BaseModel):
    """`tag` has no effect on `FakeTraceMethod.create_source` (which
    always returns whatever `TraceSource` it was built or last mutated to
    return) -- it exists purely so two instances can compare unequal,
    which is what `RuntimeChannel.apply_config` uses to decide "this
    method's settings changed, restart it." Tests that don't care about
    restart semantics just use the default."""

    tag: str = ""


class FakeTraceMethod:
    """Test-only `TraceMethod` (Task 22) that always returns one
    pre-built `TraceSource`, so a test can drive `RuntimeChannel.
    apply_config` without a real settings schema or the real
    `TRACE_METHODS` registry. Shared here for the same reason
    `ScriptedTraceSource` is: both `tests/runtime/test_channel.py` and
    `tests/api/test_runtime_ws.py` need it."""

    settings_model: type[BaseModel] = FakeTraceMethodSettings

    def __init__(self, source: TraceSource, *, method_id: str = "fake") -> None:
        self.id = method_id
        self._source = source

    def info(self) -> TraceMethodInfo:
        return TraceMethodInfo(
            id=self.id,
            label=f"Fake ({self.id})",
            description="Test-only method.",
            warning=None,
            live_state=True,
            setup_hint=None,
            settings_schema=FakeTraceMethodSettings.model_json_schema(),
        )

    def create_source(self, settings: BaseModel) -> TraceSource:
        return self._source
