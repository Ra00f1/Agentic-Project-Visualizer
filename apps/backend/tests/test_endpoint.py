"""FastAPI endpoint smoke test.

Uses `httpx.AsyncClient` + FastAPI's ASGI transport to hit /graph without
starting uvicorn. Also skipped when Mongo isn't reachable.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from agentic_visualizer.main import create_app


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    """No DB dependency — should always pass."""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


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
