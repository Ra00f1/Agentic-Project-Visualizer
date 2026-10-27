"""End-to-end test for the runtime overlay (Task 21): a real trace file,
tailed by a real `FileTailTraceSource`, resolved by a real `RuntimeChannel`,
streamed over a real `/ws/runtime` WebSocket -- the full pipeline this
task's bug (dead letters silently not appearing) lives in. Every other test
in this suite exercises one layer at a time with fakes; this one is the
check that the layers actually wire together.

No Mongo needed -- `create_app(Settings(...))` is built directly, with
`default_trace_log_path` pointed at a `tmp_path` file that does NOT exist
yet when the app starts (the exact shape Fix 1 fixes: the target creates
the file only once it starts tracing). No session token is set, so auth is
off (Task 20's D2) -- this test is about the runtime pipeline, not auth.

The trace events come from a REAL subprocess running the REAL `apv_trace`
package (already a backend dev dependency -- `tests/trace/
test_apv_trace_contract.py` uses the same approach, see its own docstring
for why a subprocess rather than an in-process import: `apv_trace` keeps
process-wide OTel state that a direct import would entangle with this
test process's own).

`base_url="http://127.0.0.1"` and `headers={"host": "127.0.0.1"}` on every
TestClient/WS call: `TrustedHostMiddleware` checks the Host header
unconditionally, and TestClient's WS path ignores `base_url` for it (see
`tests/api/test_runtime_ws.py`'s docstring for the empirically-confirmed
reason).
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient, WebSocketTestSession

from agentic_visualizer.config import Settings
from agentic_visualizer.domain import Graph, Node, Provenance
from agentic_visualizer.main import create_app

_TS = datetime(2026, 1, 1, tzinfo=UTC)
_OVERALL_TIMEOUT_SECONDS = 10.0

# Two `@apv_trace.trace`-d calls -- "alpha_tool" and "beta_tool" -- each
# emitting a start+end pair, for 4 lines total. Run as a subprocess so the
# REAL emitter (not a hand-built fixture) produces the file this test tails.
_EMIT_SCRIPT = """
from pathlib import Path
import apv_trace

apv_trace.configure(Path({log_path!r}))


@apv_trace.trace(kind="tool", name="alpha_tool")
def alpha():
    pass


@apv_trace.trace(kind="tool", name="beta_tool")
def beta():
    pass


alpha()
beta()
"""


def _run_emitter(log_path: Path) -> None:
    script = _EMIT_SCRIPT.format(log_path=str(log_path))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def _collect_until(
    ws: WebSocketTestSession,
    predicate: Callable[[dict[str, Any]], bool],
    timeout: float = _OVERALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Read WS JSON messages until one satisfies `predicate`, bounded by an
    OVERALL deadline (not a per-message one) -- `WebSocketTestSession.
    receive_json` has no timeout parameter of its own (it's a blocking
    `portal.call`), so each read is wrapped in a thread with whatever time
    remains. A real timeout always ends the test via `pytest.fail` rather
    than looping again -- retrying after a timeout would leave the earlier
    blocked read racing a new one for the next queued message.

    Not a `with ThreadPoolExecutor(...)` block: on a genuine hang (the
    pipeline wedges completely and NO message ever arrives again), the one
    submitted `receive_json()` call blocks forever with no way to cancel a
    running thread -- `shutdown(wait=True)` (what `__exit__` does) would
    then block this function's own `pytest.fail()` from ever completing,
    turning the "10-second timeout" this docstring promises into an actual
    hang. `shutdown(wait=False)` in `finally` lets `pytest.fail` raise
    immediately either way; the orphaned thread is harmless -- it unblocks
    (or is simply abandoned) once the enclosing `with TestClient(...)`
    closes the app/WS right after this test function returns.
    """
    deadline = time.monotonic() + timeout
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pytest.fail("timed out waiting for a matching WS message")
            try:
                message: dict[str, Any] = pool.submit(ws.receive_json).result(timeout=remaining)
            except FutureTimeoutError:
                pytest.fail("timed out waiting for a matching WS message")
            if predicate(message):
                return message
    finally:
        pool.shutdown(wait=False)


def test_dead_letters_and_stats_cover_every_unresolved_event(tmp_path: Path) -> None:
    """No graph is ever loaded, so all 4 emitted events are unresolved:
    `count` must reach 4, and the `apv_live_file` entry in `sources` must
    report `eventsReceived == 4` and `state == "running"` -- the signals
    Fix 3 (Task 21) and Task 22's per-source status add specifically so
    this isn't indistinguishable from the overlay being off."""
    log_path = tmp_path / "trace.jsonl"
    assert not log_path.exists()  # Fix 1's exact precondition

    app = create_app(Settings(default_trace_log_path=str(log_path)))
    with TestClient(app, base_url="http://127.0.0.1") as client, client.websocket_connect(
        "/ws/runtime", headers={"host": "127.0.0.1"}
    ) as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"

        _run_emitter(log_path)

        stats = _collect_until(ws, lambda m: m["type"] == "dead_letter_stats" and m["count"] == 4)
        assert stats["eventsResolved"] == 0
        assert len(stats["sources"]) == 1
        source = stats["sources"][0]
        assert source["methodId"] == "apv_live_file"
        assert source["state"] == "running"
        assert source["detail"] == str(log_path)
        assert source["eventsReceived"] == 4
        assert source["eventsIgnored"] == 4


def test_update_graph_lets_matching_events_resolve_and_publish_a_delta(tmp_path: Path) -> None:
    """A graph with a node named "alpha_tool" is loaded BEFORE the same
    4-event burst runs: alpha's start+end now resolve (a delta for that
    node), beta's start+end still don't -- `count` lands on exactly 2
    (4 - 2 resolved), not 4."""
    log_path = tmp_path / "trace.jsonl"
    node = Node(
        id="mongo:db.tools:1",
        type="tool",
        name="alpha_tool",
        provenance=Provenance(source="test", source_ref="test:1", scanned_at=_TS),
    )

    app = create_app(Settings(default_trace_log_path=str(log_path)))
    with TestClient(app, base_url="http://127.0.0.1") as client, client.websocket_connect(
        "/ws/runtime", headers={"host": "127.0.0.1"}
    ) as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"

        assert client.portal is not None
        client.portal.call(app.state.runtime_channel.update_graph, Graph(nodes=[node]))
        reset = _collect_until(ws, lambda m: m["type"] == "reset")
        assert reset["type"] == "reset"

        _run_emitter(log_path)

        # Its start AND end both resolve within the same ~50ms batch
        # window (the subprocess runs fast), so `_pending`'s per-node
        # coalescing (see `RuntimeChannel`'s module docstring) likely
        # publishes only the FINAL delta -- "idle, ran once" -- not a
        # separate transient "active" one. `runCount` is the unambiguous
        # signal either way: it only increments on a completed run.
        delta = _collect_until(ws, lambda m: m["type"] == "delta" and m["nodeId"] == node.id)
        assert delta["runCount"] == 1

        stats = _collect_until(ws, lambda m: m["type"] == "dead_letter_stats" and m["count"] == 2)
        assert stats["eventsResolved"] == 2
        assert len(stats["sources"]) == 1
        source = stats["sources"][0]
        assert source["state"] == "running"
        assert source["eventsReceived"] == 4
        assert source["eventsIgnored"] == 2
