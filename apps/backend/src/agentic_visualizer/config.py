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

from pydantic import Field
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
            "tauri://localhost",       # Tauri prod webview
        ],
    )
    """Origins allowed to hit the sidecar. Deliberately tight — no wildcard,
    no `*`. If the frontend port changes, this list has to change with it."""

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


def get_settings() -> Settings:
    """Return a fresh Settings instance.

    Not memoized: settings are cheap to construct, and re-reading env each
    time makes the sidecar responsive to env changes without a restart
    (useful in tests, harmless in prod).
    """
    return Settings()
