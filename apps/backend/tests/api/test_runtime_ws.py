"""WebSocket integration test for /ws/runtime.

Uses Starlette's `TestClient`, not `httpx.AsyncClient` + `ASGITransport` (the
convention in `test_endpoint.py`): httpx has no WebSocket support at all.
`TestClient.websocket_connect()` is the standard way to test a
Starlette/FastAPI WS endpoint without a real running server -- it drives the
ASGI app on a background thread with its own event loop, so this file's
tests are plain `def`s, not `async def`, unlike the rest of the suite.
Using it as `with TestClient(app) as client:` also runs the app's real
lifespan (so `RuntimeChannel.start()` actually happens), and exposes
`client.portal` -- an anyio `BlockingPortal` for running a coroutine on
that same event loop from this sync test, which is how these tests drive
`RuntimeChannel.update_graph()`/`set_source()` directly (bypassing HTTP,
matching `test_channel.py`'s approach) to control what the WS sees.

No auth to test here: this endpoint has none (see `main.py`'s `/ws/runtime`
comment and Task 3's `## Result` for why) -- the handshake is a plain
WebSocket upgrade.
"""

from __future__ import annotations

from datetime import UTC, datetime

from starlette.testclient import TestClient

from agentic_visualizer.domain import Graph, Node, Provenance
from agentic_visualizer.main import create_app
from agentic_visualizer.runtime.channel import RuntimeChannel
from agentic_visualizer.trace.events import TraceEvent, TraceRef
from tests.conftest import ScriptedTraceSource as _ScriptedSource

_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _node() -> Node:
    return Node(
        id="mongo:db.tools:1",
        type="tool",
        name="foo",
        provenance=Provenance(source="test", source_ref="test:1", scanned_at=_TS),
    )


def _event() -> TraceEvent:
    return TraceEvent(
        v=1, ts=_TS, run_id="r1", span_id="s1", phase="start", kind="tool",
        ref=TraceRef(name="foo", module="pkg.mod"),
    )


def test_connect_receives_a_snapshot_first() -> None:
    app = create_app()
    with TestClient(app) as client, client.websocket_connect("/ws/runtime") as ws:
        message = ws.receive_json()
        assert message["type"] == "snapshot"
        assert message["states"] == []


def test_delta_and_reset_reach_a_connected_client() -> None:
    app = create_app()
    runtime_channel: RuntimeChannel = app.state.runtime_channel

    with TestClient(app) as client, client.websocket_connect("/ws/runtime") as ws:
        assert client.portal is not None  # only None outside the `with TestClient(...)` block
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"

        client.portal.call(runtime_channel.update_graph, Graph(nodes=[_node()]))
        reset_message = ws.receive_json()
        assert reset_message["type"] == "reset"

        client.portal.call(runtime_channel.set_source, _ScriptedSource([_event()]))
        delta = ws.receive_json()
        assert delta["type"] == "delta"
        assert delta["nodeId"] == "mongo:db.tools:1"
        assert delta["status"] == "active"
        assert delta["runCount"] == 1
