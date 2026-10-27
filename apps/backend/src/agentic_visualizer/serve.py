"""`python -m agentic_visualizer.serve` — the entry point Tauri spawns (task 20).

Binds an ephemeral loopback port, generates a per-launch session token,
starts uvicorn on that socket, and once it's actually accepting
connections, prints exactly one JSON handshake line to **stdout** and
flushes it. Everything else (uvicorn's own logging, this module's own)
goes to stderr, so a parent process reading stdout line-by-line sees
nothing but the handshake.

Why a bound socket plus a handshake line, not the parent picking a port
and handing it to us: binding here means there's no window where the
parent's choice and this process's bind could race (another process
grabbing the port in between). The handshake line doubles as the
readiness signal — nothing downstream needs to poll `/health`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import socket
import sys
import threading
from typing import Any

import uvicorn
from pydantic import SecretStr

from .config import Settings
from .main import create_app

logger = logging.getLogger("agentic_visualizer.serve")

_POLL_INTERVAL_SECONDS = 0.05


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m agentic_visualizer.serve")
    parser.add_argument("--port", type=int, default=0, help="Loopback port to bind (0 = ephemeral).")
    parser.add_argument(
        "--watch-stdin",
        action="store_true",
        help="Exit gracefully when stdin closes (the parent process died or closed the pipe).",
    )
    parser.add_argument(
        "--token-from-env",
        action="store_true",
        help="Read the session token from AGENTIC_SESSION_TOKEN instead of generating one.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    if args.token_from_env:
        token = os.environ.get("AGENTIC_SESSION_TOKEN")
        if not token:
            print("error: --token-from-env given but AGENTIC_SESSION_TOKEN is not set", file=sys.stderr)
            return 1
    else:
        token = secrets.token_hex(32)

    return asyncio.run(_serve(port=args.port, token=token, watch_stdin=args.watch_stdin))


async def _serve(*, port: int, token: str, watch_stdin: bool) -> int:
    # Bound before uvicorn starts, so `sock.getsockname()` already has the
    # real ephemeral port -- uvicorn never gets a chance to pick its own.
    #
    # Deliberately no SO_REUSEADDR: `port` is always 0 here (a fresh
    # ephemeral bind), so there's no prior TIME_WAIT socket to reuse, and
    # the flag has no POSIX benefit to buy. On Windows it means something
    # different and actively bad -- it lets ANOTHER process bind the same
    # address while this one is still listening (port hijacking), which
    # would undermine the whole point of this handshake: a hostile local
    # process could steal the port and intercept or spoof the backend.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", port))
    actual_port = sock.getsockname()[1]

    settings = Settings(session_token=SecretStr(token))
    app = create_app(settings=settings)
    config = uvicorn.Config(app, access_log=False, log_level="info", lifespan="on")
    server = uvicorn.Server(config)

    serve_task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started and not serve_task.done():
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    if serve_task.done():
        exc = serve_task.exception()
        if exc is not None:
            logger.error("backend failed to start: %s: %s", exc.__class__.__name__, exc)
        return 1

    handshake: dict[str, Any] = {"port": actual_port, "token": token, "version": app.version, "pid": os.getpid()}
    print(f"APV_BACKEND_READY {json.dumps(handshake)}", flush=True)

    if watch_stdin:
        _start_stdin_watcher(server, asyncio.get_running_loop())

    await serve_task
    return 0


def _start_stdin_watcher(server: uvicorn.Server, loop: asyncio.AbstractEventLoop) -> None:
    """Background daemon thread, flagged per CLAUDE.md §12: blocks on
    `sys.stdin.buffer.read()` until EOF, then tells the server to shut
    down gracefully via `call_soon_threadsafe` (the textbook-safe way to
    touch asyncio state from another thread, even though a plain
    attribute set would also work under the GIL).

    Why: if the parent process (Tauri) dies for any reason -- crash, a
    Task Manager kill -- the pipe closes and this process exits on its
    own. No orphaned Python process, and it works through the `uv run` ->
    python chain in dev too, since python inherits stdin either way.
    """

    def _watch() -> None:
        sys.stdin.buffer.read()
        loop.call_soon_threadsafe(setattr, server, "should_exit", True)

    threading.Thread(target=_watch, name="apv-stdin-watcher", daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
