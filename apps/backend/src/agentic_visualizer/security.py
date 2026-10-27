"""Session-token auth (task 20).

Every endpoint on this sidecar was open until now — see `main.py`'s own
module docstring and `config.py`'s `session_token` field for why. This
module holds the actual checks; `main.py` wires them into the middleware
stack and the one WebSocket route.

**HTTP** goes through `TokenAuthMiddleware`, a small pure-ASGI middleware
(not `BaseHTTPMiddleware` — that wraps the response in a way that breaks
streaming and adds overhead neither endpoint here needs). **WebSocket**
can't set a custom header from a browser, so the token travels as a
`Sec-WebSocket-Protocol` entry instead — `websocket_subprotocol_for_token`
is the one place that's checked, called directly from `main.py`'s
`/ws/runtime` route before `accept()`.
"""

from __future__ import annotations

import hmac
import logging

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("agentic_visualizer.security")

# The subprotocol the server always echoes back on a successful WS
# handshake — never the token subprotocol itself, which must never appear
# anywhere it could be logged (a Sec-WebSocket-Protocol RESPONSE header is
# far more likely to be logged by a proxy or dev tool than the one in the
# request, so echoing a value that already contains the token would be a
# real leak, not just a style nit).
WS_PROTOCOL_MARKER = "apv.v1"
_WS_TOKEN_SUBPROTOCOL_PREFIX = "apv.token."


class TokenAuthMiddleware:
    """Bearer-token check for every HTTP request except `OPTIONS`.

    `OPTIONS` is skipped because a CORS preflight request carries no
    `Authorization` header by design (the browser builds it itself, before
    the real request, specifically to ask "would this be allowed" without
    sending the real request's headers) — rejecting it here would make
    every real cross-origin request fail at the preflight stage, before
    the browser ever got to send the token on the real request.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        if not _bearer_token_matches(scope, self._token):
            response = JSONResponse({"detail": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _bearer_token_matches(scope: Scope, token: str) -> bool:
    raw_value = b""
    for name, value in scope["headers"]:
        if name == b"authorization":
            raw_value = value
            break
    try:
        header = raw_value.decode("latin-1")
    except UnicodeDecodeError:
        return False
    return hmac.compare_digest(header, f"Bearer {token}")


def websocket_subprotocol_for_token(offered: list[str], token: str) -> str | None:
    """Returns `WS_PROTOCOL_MARKER` to accept with if `offered` (the
    client's `Sec-WebSocket-Protocol` list) contains a valid
    `apv.token.<token>` entry, checked with `hmac.compare_digest`
    (constant-time: the client can't learn how much of its guess matched
    from response timing). `None` means reject."""
    expected = f"{_WS_TOKEN_SUBPROTOCOL_PREFIX}{token}"
    for candidate in offered:
        if hmac.compare_digest(candidate, expected):
            return WS_PROTOCOL_MARKER
    return None


def websocket_origin_is_allowed(origin: str | None, allowed_origins: list[str]) -> bool:
    """A missing `Origin` header (non-browser clients, including every
    test in this suite) is allowed — only a PRESENT, non-matching Origin
    is rejected. A browser always sends `Origin` on a cross-origin
    WebSocket handshake, so this still blocks the attack it exists for
    (an arbitrary web page opening a WS connection to this sidecar)."""
    return origin is None or origin in allowed_origins
