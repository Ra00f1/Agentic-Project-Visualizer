"""Tests for session-token auth (task 20): HTTP bearer token, CORS/401
interaction, `TrustedHostMiddleware`, and the `/ws/runtime` origin +
subprotocol checks.

Uses the same `httpx.AsyncClient` + `ASGITransport` pattern as
`test_endpoint.py` for HTTP, and `starlette.testclient.TestClient` for
WebSocket (see that file's own docstring for why). `base_url="http://127.0.0.1"`
and `headers={"host": "127.0.0.1"}` on WS connects are both required for
the same reason `test_endpoint.py`/`test_runtime_ws.py` need them:
`TrustedHostMiddleware` now checks every request's `Host` header.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agentic_visualizer.config import Settings
from agentic_visualizer.main import create_app
from agentic_visualizer.security import WS_PROTOCOL_MARKER

_TOKEN = "a" * 64
_WRONG_TOKEN = "b" * 64


def _settings_with_token() -> Settings:
    return Settings(session_token=_TOKEN)


# ---------------------------------------------------------------------------
# Verification 1: HTTP bearer token


@pytest.mark.asyncio
async def test_no_authorization_header_is_401() -> None:
    app = create_app(settings=_settings_with_token())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        r = await client.get("/health")
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}


@pytest.mark.asyncio
async def test_wrong_token_is_401() -> None:
    app = create_app(settings=_settings_with_token())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        r = await client.get("/health", headers={"Authorization": f"Bearer {_WRONG_TOKEN}"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_correct_token_reaches_health_and_dead_letter() -> None:
    app = create_app(settings=_settings_with_token())
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        health_response = await client.get("/health", headers=headers)
        dead_letter_response = await client.get("/runtime/dead-letter", headers=headers)
    assert health_response.status_code == 200
    assert dead_letter_response.status_code == 200


@pytest.mark.asyncio
async def test_correct_token_reaches_schema(require_mongo) -> None:
    """Verification 1's own wording says `POST /schema`; the real route
    is `GET /schema` (confirmed by reading `main.py` -- no POST variant
    exists, and adding one is outside this task's Scope/Files). Testing
    the real route rather than inventing a second one."""
    uri, db = require_mongo
    app = create_app(settings=_settings_with_token())
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        r = await client.get("/schema", params={"uri": uri, "db": db}, headers=headers)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Verification 2 & 3: CORS preflight and 401-carries-CORS-headers


@pytest.mark.asyncio
async def test_options_preflight_passes_without_a_token() -> None:
    app = create_app(settings=_settings_with_token())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        r = await client.options(
            "/health",
            headers={
                "Origin": "http://localhost:1420",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert r.status_code < 400
    assert r.headers.get("access-control-allow-origin") == "http://localhost:1420"


@pytest.mark.asyncio
async def test_401_response_still_carries_cors_headers() -> None:
    app = create_app(settings=_settings_with_token())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        r = await client.get("/health", headers={"Origin": "http://localhost:1420"})
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") == "http://localhost:1420"


# ---------------------------------------------------------------------------
# Verification 4: TrustedHostMiddleware


@pytest.mark.asyncio
async def test_unexpected_host_header_is_400() -> None:
    app = create_app(settings=_settings_with_token())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        r = await client.get("/health", headers={"Host": "evil.example"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Verification 5: WebSocket origin + subprotocol token
#
# `websocket.close()` called BEFORE `.accept()` raises `WebSocketDisconnect`
# here, not `WebSocketDenialResponse` -- that second one is what a REAL
# uvicorn server turns a pre-accept close into (a 403 handshake rejection,
# per the Design's own note), but `TestClient` drives the ASGI app
# directly and sees the raw `"websocket.close"` message instead. Confirmed
# empirically: asserting on the wrong exception type here would make every
# "rejected" test below pass for the wrong reason (any exception at all).


def _ws_client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.mark.asyncio
async def test_ws_no_subprotocols_is_rejected() -> None:
    app = create_app(settings=_settings_with_token())
    with pytest.raises(WebSocketDisconnect), _ws_client(app) as client, client.websocket_connect(
        "/ws/runtime", headers={"host": "127.0.0.1"}
    ):
        pass


@pytest.mark.asyncio
async def test_ws_wrong_token_is_rejected() -> None:
    app = create_app(settings=_settings_with_token())
    with pytest.raises(WebSocketDisconnect), _ws_client(app) as client, client.websocket_connect(
        "/ws/runtime",
        subprotocols=["apv.v1", f"apv.token.{_WRONG_TOKEN}"],
        headers={"host": "127.0.0.1"},
    ):
        pass


@pytest.mark.asyncio
async def test_ws_correct_token_is_accepted_and_snapshot_arrives() -> None:
    app = create_app(settings=_settings_with_token())
    with _ws_client(app) as client, client.websocket_connect(
        "/ws/runtime",
        subprotocols=["apv.v1", f"apv.token.{_TOKEN}"],
        headers={"host": "127.0.0.1"},
    ) as ws:
        assert ws.accepted_subprotocol == WS_PROTOCOL_MARKER
        message = ws.receive_json()
        assert message["type"] == "snapshot"


@pytest.mark.asyncio
async def test_ws_correct_token_with_disallowed_origin_is_rejected() -> None:
    app = create_app(settings=_settings_with_token())
    with pytest.raises(WebSocketDisconnect), _ws_client(app) as client, client.websocket_connect(
        "/ws/runtime",
        subprotocols=["apv.v1", f"apv.token.{_TOKEN}"],
        headers={"host": "127.0.0.1", "origin": "https://evil.example"},
    ):
        pass


@pytest.mark.asyncio
async def test_ws_correct_token_with_allowed_origin_is_accepted() -> None:
    app = create_app(settings=_settings_with_token())
    with _ws_client(app) as client, client.websocket_connect(
        "/ws/runtime",
        subprotocols=["apv.v1", f"apv.token.{_TOKEN}"],
        headers={"host": "127.0.0.1", "origin": "http://localhost:1420"},
    ) as ws:
        assert ws.accepted_subprotocol == WS_PROTOCOL_MARKER


# ---------------------------------------------------------------------------
# Verification 6: session_token=None -> auth off, exactly one WARNING


@pytest.mark.asyncio
async def test_no_token_configured_disables_auth_with_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="agentic_visualizer"):
        app = create_app(settings=Settings(session_token=None))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "session token" in warnings[0].message

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        r = await client.get("/health")
    assert r.status_code == 200
