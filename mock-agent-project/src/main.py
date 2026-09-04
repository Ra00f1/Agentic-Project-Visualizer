"""FastAPI app entrypoint for the mock project.

Two things the scanner should notice:

  1. `app.include_router(router)` — the endpoint extractor must follow the
     include and attribute the routes to `api/routes.py`, not to `main.py`.
  2. A direct `@app.get("/")` on the app itself — proves both attribute
     styles work.
"""

from __future__ import annotations

from fastapi import FastAPI

from .api.routes import router


app = FastAPI(title="mock-agent-project", version="0.0.0")
app.include_router(router)


@app.get("/")
def root() -> dict[str, str]:
    """Root endpoint attached directly to the app."""
    return {"service": "mock-agent-project", "ok": "true"}


@app.get("/version")
def version() -> dict[str, str]:
    """Simple version endpoint."""
    return {"version": app.version}
