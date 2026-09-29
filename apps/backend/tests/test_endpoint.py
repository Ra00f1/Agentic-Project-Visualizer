"""FastAPI endpoint smoke test.

Uses `httpx.AsyncClient` + FastAPI's ASGI transport to hit /graph without
starting uvicorn. Also skipped when Mongo isn't reachable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from agentic_visualizer.main import create_app
from agentic_visualizer.trace.events import TraceEvent, TraceRef

# <repo>/mock-agent-project — same fixture, same portable-path pattern as
# test_l2_scanner.py's FIXTURE_ROOT.
_FIXTURE_CODEBASE_ROOT = Path(__file__).parents[3] / "mock-agent-project"


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    """No DB dependency — should always pass."""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_dead_letter_endpoint_returns_empty_list_with_no_activity() -> None:
    """No DB dependency, no runtime channel activity — the DeadLetterDrawer's
    fetch must succeed with an empty list rather than 404/500."""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/runtime/dead-letter")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_dead_letter_endpoint_reflects_recorded_entries() -> None:
    app = create_app()
    app.state.runtime_channel.dead_letter.record(
        _unresolvable_event(),
        "no node matches ref",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/runtime/dead-letter")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["kind"] == "tool"
    assert body[0]["refName"] == "some_tool"
    assert body[0]["reason"] == "no node matches ref"


def _unresolvable_event() -> TraceEvent:
    return TraceEvent(
        v=1,
        ts=datetime.now(UTC),
        run_id="run-1",
        span_id="span-1",
        phase="start",
        kind="tool",
        ref=TraceRef(name="some_tool", module="pkg.mod"),
    )


@pytest.mark.asyncio
async def test_graph_endpoint_returns_populated_graph(require_mongo) -> None:
    """Smoke test — the endpoint round-trips a populated L1 graph.

    Deliberately assertion-on-shape, not exact counts: total node/edge
    counts belong in test_l1_scanner.py where the arithmetic breakdown
    is documented alongside them. Coupling this smoke test to exact
    numbers means every fixture change ripples here for no gain.

    L2 / L3 aren't invoked because this test doesn't pass ?codebaseRoot,
    so we only assert on the L1 shape. When L2/L3 are exercised, the
    dedicated test_l2_scanner.py / test_l3_scanner.py verify them.
    """
    uri, db = require_mongo
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/graph", params={"uri": uri, "db": db})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["nodes"], list) and len(body["nodes"]) > 0
    assert isinstance(body["edges"], list) and len(body["edges"]) > 0

    # Every L1 type the mapping covers is represented at least once —
    # a smoke check that the pipeline didn't silently drop a collection.
    types_seen = {n["type"] for n in body["nodes"]}
    for expected in ("workflow", "agent", "model", "tool"):
        assert expected in types_seen, f"missing type {expected!r} in {types_seen}"

    # Node shape — every entry carries the fields the frontend needs.
    for node in body["nodes"]:
        assert set(node.keys()) >= {"id", "type", "name", "provenance", "attributes"}

    # Edge shape — same idea, plus the "uses" kind is guaranteed by L1
    # (every workflow uses agents, every agent uses a model).
    kinds_seen = {e["kind"] for e in body["edges"]}
    assert "uses" in kinds_seen, f"expected 'uses' edges, got {kinds_seen}"


@pytest.mark.asyncio
async def test_graph_endpoint_returns_function_nodes_when_codebase_root_supplied(
    require_mongo,
) -> None:
    """Regression test for task 5: the frontend's tool -> function expansion
    has nothing to show unless L2 actually ran, which only happens when
    `codebaseRoot` is supplied. `mock_agent`'s tools reference real functions
    in the `mock-agent-project` fixture, so at least one should resolve.

    This complements test_l2_scanner.py (which exercises L2 directly against
    the codebase adapter) by proving the same thing round-trips through the
    actual HTTP endpoint the frontend calls.
    """
    assert _FIXTURE_CODEBASE_ROOT.exists(), f"fixture missing: {_FIXTURE_CODEBASE_ROOT}"
    uri, db = require_mongo
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(
            "/graph",
            params={"uri": uri, "db": db, "codebaseRoot": str(_FIXTURE_CODEBASE_ROOT)},
        )
    assert r.status_code == 200
    body = r.json()
    function_nodes = [n for n in body["nodes"] if n["type"] == "function"]
    assert len(function_nodes) > 0, "expected at least one function node with codebaseRoot set"
    implements_edges = [e for e in body["edges"] if e["kind"] == "implements"]
    assert len(implements_edges) > 0, "expected at least one 'implements' edge with codebaseRoot set"


@pytest.mark.asyncio
async def test_graph_endpoint_survives_runtime_channel_update_failure(
    require_mongo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug in the runtime overlay (a secondary, opt-in feature wired via
    RuntimeChannel.update_graph -- see runtime/channel.py) must not turn an
    otherwise-successful /graph scan into a failed HTTP response."""
    uri, db = require_mongo
    app = create_app()

    async def _boom(graph: object) -> None:
        raise RuntimeError("simulated runtime-overlay bug")

    monkeypatch.setattr(app.state.runtime_channel, "update_graph", _boom)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/graph", params={"uri": uri, "db": db})

    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["nodes"], list) and len(body["nodes"]) > 0
    for edge in body["edges"]:
        assert set(edge.keys()) >= {"sourceId", "targetId", "kind", "provenance"}


@pytest.mark.asyncio
async def test_graph_endpoint_502s_on_bad_uri() -> None:
    """Unreachable Mongo should surface as 502, not a stack trace."""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Port 1 is a well-known "nothing listens here" — fast connection refused.
        r = await client.get(
            "/graph",
            params={"uri": "mongodb://127.0.0.1:1", "db": "mock_agent"},
        )
    assert r.status_code == 502
    assert "Mongo error" in r.json()["detail"]
