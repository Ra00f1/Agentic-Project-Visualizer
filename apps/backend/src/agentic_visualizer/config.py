"""Runtime configuration.

Kept small on purpose. The visualizer is meant to be pointed at a different
DB / codebase every time the user asks it to, so most "configuration" arrives
as request parameters, not env vars. What lives here is the host/port the
sidecar listens on, CORS origins, and defaults the user hasn't overridden yet.

`pydantic-settings` picks up any of these from the process env with an
`AGENTIC_` prefix — e.g. `AGENTIC_HOST=0.0.0.0`. That matches how the Tauri
shell will inject values into the sidecar process at launch.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTIC_",
        env_file=None,
        extra="ignore",
    )

    host: str = "127.0.0.1"
    """Loopback only by default — the sidecar is not meant to be reachable off-box."""

    port: int = 8765
    """Chosen to be memorable. Tauri will pass a real ephemeral port at launch;
    this default is only for `uvicorn ... --reload` during dev."""

    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:1420",  # Tauri dev server default
            "tauri://localhost",       # Tauri prod webview (macOS/Linux)
            "http://tauri.localhost",  # Tauri prod webview (Windows, useHttpsScheme=false)
        ],
    )
    """Origins allowed to hit the sidecar. Deliberately tight — no wildcard,
    no `*`. If the frontend port changes, this list has to change with it."""

    session_token: SecretStr | None = None
    """Per-launch bearer token (task 20). `None` means auth is OFF — logged
    once as a WARNING by `create_app`, and only reachable by running
    `uvicorn agentic_visualizer.main:app` by hand or the existing test
    suite; the installed app always launches through `serve.py`, which
    always generates one. `SecretStr` so a stray `repr(settings)` in a log
    line never leaks it — callers that need the raw value call
    `.get_secret_value()` explicitly."""

    default_mongo_uri: str = "mongodb://localhost:27017"
    """Fallback URI when the client didn't specify one. Convenience only."""

    default_codebase_root: str | None = None
    """Filesystem path used by L2 (and later L3/L4) when the client didn't
    specify a codebase root. Optional: when unset, L2 is skipped and only
    L1 runs. In production the setup screen supplies the path per-scan; the
    default is here so the sidecar in dev / tests can point at the
    fixture project by env (`AGENTIC_DEFAULT_CODEBASE_ROOT=...`) without a
    code change. Never resolved from CWD or from Python's import path —
    always a caller-supplied absolute or explicitly-relative path."""

    default_trace_log_path: str | None = None
    """Filesystem path to a JSONL trace log (written by a target project's
    `apv_trace` helper — see `packages/apv_trace`) to tail automatically
    at startup. Optional: when unset, the runtime
    overlay stays inert until something else points `RuntimeChannel` at a
    source. Matches CLAUDE.md §1's "off unless the user explicitly enables
    it" — there's no setup-screen control for this yet, so env
    (`AGENTIC_DEFAULT_TRACE_LOG_PATH=...`) is the only way to turn it on
    today."""

    orphaned_span_ttl_seconds: float = 300.0
    """How long (seconds) a trace span can sit `active` with no matching
    `end`/`error` event before the runtime overlay's orphan-sweep loop
    evicts it and flips the node back to `idle` -- protects against a target
    project crashing (or being killed) mid-span and leaving a node stuck
    `active` forever. 300s (5 min) is a conservative guess, not a measured
    value; override with `AGENTIC_ORPHANED_SPAN_TTL_SECONDS` if a real
    target project's legitimately-slow calls get evicted prematurely."""


def get_settings() -> Settings:
    """Return a fresh Settings instance.

    Not memoized: settings are cheap to construct, and re-reading env each
    time makes the sidecar responsive to env changes without a restart
    (useful in tests, harmless in prod).
    """
    return Settings()
