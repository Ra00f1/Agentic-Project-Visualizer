"""Tests for `GET /runtime/methods`, `GET /runtime/config`, and
`PUT /runtime/config` (Task 22).

`starlette.testclient.TestClient`, not `httpx.AsyncClient` + `ASGITransport`
(`test_endpoint.py`'s convention elsewhere in this suite): several tests
here depend on the app's real lifespan running (the env-var startup
default, in particular) and on a source's own background consume task
staying alive and scheduled ACROSS several sequential requests -- neither
is guaranteed under `ASGITransport`, which doesn't drive the ASGI lifespan
protocol. `with TestClient(app) as client:` does, and
`tests/api/test_runtime_ws.py`'s own docstring documents exactly this
(confirmed empirically there first). `TestClient` is sync, so these tests
are plain `def`s, not `async def`.

`base_url="http://127.0.0.1"`: `TrustedHostMiddleware` checks the `Host`
header against `["127.0.0.1", "localhost"]` unconditionally.
"""

from __future__ import annotations

import time
from pathlib import Path

from starlette.testclient import TestClient

from agentic_visualizer.config import Settings
from agentic_visualizer.main import create_app


def test_put_runtime_config_passes_cors_preflight() -> None:
    """Regression test: `CORSMiddleware`'s `allow_methods` originally only
    listed `GET`/`POST` (from before this endpoint existed), which silently
    failed a REAL browser's preflight `OPTIONS` request for `PUT` --
    invisible to `TestClient.put()`/`AsyncClient.put()` calls elsewhere in
    this file, since those skip the preflight step entirely and go
    straight to the real request. Caught only by driving the app from an
    actual browser (Task 22's own manual verification step)."""
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.options(
            "/runtime/config",
            headers={"Origin": "http://localhost:1420", "Access-Control-Request-Method": "PUT"},
        )
    assert r.status_code < 400
    assert "PUT" in r.headers.get("access-control-allow-methods", "")


def test_get_runtime_methods_lists_apv_live_file_with_warning_and_live_state() -> None:
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.get("/runtime/methods")
    assert r.status_code == 200
    methods = r.json()
    apv_live_file = next(m for m in methods if m["id"] == "apv_live_file")
    assert apv_live_file["warning"] is not None
    assert apv_live_file["liveState"] is True


def test_put_with_apv_live_file_runs_and_events_reach_dead_letter_counts(tmp_path: Path) -> None:
    log_path = tmp_path / "trace.jsonl"
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        put_response = client.put(
            "/runtime/config", json={"methods": [{"id": "apv_live_file", "settings": {"path": str(log_path)}}]}
        )
        assert put_response.status_code == 200
        body = put_response.json()
        assert body["methods"] == [{"id": "apv_live_file", "settings": {"path": str(log_path)}}]
        assert len(body["sources"]) == 1
        assert body["sources"][0]["methodId"] == "apv_live_file"
        assert body["sources"][0]["state"] == "waiting"  # file doesn't exist yet

        # Write one valid trace line directly -- no need for a real
        # apv_trace subprocess here (that's test_runtime_end_to_end.py's
        # job); this test is about the endpoint's wiring, not the emitter.
        line = (
            '{"v":1,"ts":"2026-01-01T00:00:00+00:00","run_id":"r1","span_id":"s1",'
            '"parent_span_id":null,"phase":"start","kind":"tool",'
            '"ref":{"name":"foo","module":"m","qualname":null,"framework_id":null,"db_id":null},'
            '"meta":{}}\n'
        )
        log_path.write_text(line, encoding="utf-8")

        deadline = time.monotonic() + 10.0
        dead_letter_count = 0
        while time.monotonic() < deadline:
            r = client.get("/runtime/dead-letter")
            dead_letter_count = len(r.json())
            if dead_letter_count > 0:
                break
            time.sleep(0.05)
        assert dead_letter_count == 1, "the event never reached the dead-letter buffer"


def test_put_with_an_empty_list_stops_the_running_source(tmp_path: Path) -> None:
    log_path = tmp_path / "trace.jsonl"
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.put("/runtime/config", json={"methods": [{"id": "apv_live_file", "settings": {"path": str(log_path)}}]})

        r = client.put("/runtime/config", json={"methods": []})

        assert r.status_code == 200
        body = r.json()
        assert body["methods"] == []
        assert body["sources"] == []


def test_put_with_changed_settings_restarts_only_that_source(tmp_path: Path) -> None:
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.put(
            "/runtime/config", json={"methods": [{"id": "apv_live_file", "settings": {"path": str(first_path)}}]}
        )

        r = client.put(
            "/runtime/config", json={"methods": [{"id": "apv_live_file", "settings": {"path": str(second_path)}}]}
        )

        assert r.status_code == 200
        body = r.json()
        assert body["methods"] == [{"id": "apv_live_file", "settings": {"path": str(second_path)}}]
        assert body["sources"][0]["detail"] == str(second_path)


def test_put_with_an_unknown_method_id_is_422() -> None:
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.put("/runtime/config", json={"methods": [{"id": "nonexistent", "settings": {}}]})
    assert r.status_code == 422


def test_put_with_a_duplicate_method_id_is_422() -> None:
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.put(
            "/runtime/config",
            json={
                "methods": [
                    {"id": "apv_live_file", "settings": {"path": "a.jsonl"}},
                    {"id": "apv_live_file", "settings": {"path": "b.jsonl"}},
                ]
            },
        )
    assert r.status_code == 422


def test_put_with_a_wrong_type_setting_is_422_with_field_errors() -> None:
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.put("/runtime/config", json={"methods": [{"id": "apv_live_file", "settings": {"path": 12345}}]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["id"] == "apv_live_file"
    assert detail["errors"]  # pydantic's field-level error list, not just a flat string


def test_a_bad_put_does_not_disturb_a_previously_running_source(tmp_path: Path) -> None:
    """The whole request validates before anything is started/stopped --
    a 422 must leave whatever was already running untouched."""
    log_path = tmp_path / "trace.jsonl"
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.put("/runtime/config", json={"methods": [{"id": "apv_live_file", "settings": {"path": str(log_path)}}]})

        bad = client.put("/runtime/config", json={"methods": [{"id": "nonexistent", "settings": {}}]})
        assert bad.status_code == 422

        r = client.get("/runtime/config")
        assert r.json()["methods"] == [{"id": "apv_live_file", "settings": {"path": str(log_path)}}]


def test_env_var_default_shows_up_as_enabled_in_get_runtime_config(tmp_path: Path) -> None:
    """Verification 7: AGENTIC_DEFAULT_TRACE_LOG_PATH turns `apv_live_file`
    on at startup, through the SAME `apply_config` path `PUT` uses -- so
    `GET /runtime/config` reflects it without the frontend ever sending a
    `PUT` itself."""
    log_path = tmp_path / "trace.jsonl"
    app = create_app(Settings(default_trace_log_path=str(log_path)))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.get("/runtime/config")
    assert r.status_code == 200
    body = r.json()
    assert body["methods"] == [{"id": "apv_live_file", "settings": {"path": str(log_path)}}]
    assert len(body["sources"]) == 1
    assert body["sources"][0]["methodId"] == "apv_live_file"
