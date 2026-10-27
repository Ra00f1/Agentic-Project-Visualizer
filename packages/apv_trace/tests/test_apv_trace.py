"""Tests for apv_trace's own emitter logic.

`apps/backend/tests/trace/test_events.py` tests the WIRE-FORMAT SCHEMA
(agentic_visualizer.trace.events.TraceEvent parsing lines this package
writes) — deliberately a separate, independent implementation agreeing on a
shape (see this package's own README). `apps/backend/tests/trace/
test_apv_trace_contract.py` does the same in a real subprocess. This file
tests the emitter itself: that `configure()`/`trace()` actually produce
those lines correctly, including the OTel-SDK rewrite (task 13),
manifest-driven db_id population, and the `name`-parameter call-time
override (task 8).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.util._once import Once

import apv_trace


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """`apv_trace` keeps process-wide emitter state (see its own module
    docstring on why — a deliberate singleton). Reset it before each test so
    tests don't see a previous test's log path/manifest.

    Also resets OpenTelemetry's OWN process-global `TracerProvider` — a
    SEPARATE piece of global state `configure()` now touches (task 13).
    `trace.set_tracer_provider()` only succeeds once per process by design
    (the SDK silently ignores every later call) — reaching into its two
    private module attributes here is the same reset OTel's own test suite
    uses for exactly this reason; there's no public API for it, because
    "reset the global tracer provider" is a test-only concern, never a
    production one.

    Order matters: the OTel reset runs BEFORE `_EmitterState()` is
    constructed. `_EmitterState.__init__` eagerly calls `get_tracer()` —
    once a real SDK provider exists, that call returns a concrete `Tracer`
    bound DIRECTLY to that provider instance (unlike the no-op case, which
    returns a `ProxyTracer` that re-resolves the current global provider on
    every call). Construct the fresh state first and a prior test's
    already-set real provider would get captured into the new state by
    reference, instead of the clean slate this test is about to configure.
    """
    trace_api._TRACER_PROVIDER = None
    trace_api._TRACER_PROVIDER_SET_ONCE = Once()
    apv_trace._state = apv_trace._EmitterState()


def _read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_sync_function_emits_start_then_end(tmp_path: Path) -> None:
    log_path = tmp_path / "trace.jsonl"
    apv_trace.configure(log_path)

    @apv_trace.trace(kind="tool", name="lookup_kb")
    def lookup_kb(topic: str) -> str:
        return f"result for {topic}"

    result = lookup_kb("weather")

    assert result == "result for weather"
    events = _read_events(log_path)
    assert [e["phase"] for e in events] == ["start", "end"]
    assert events[0]["kind"] == "tool"
    assert events[0]["ref"]["name"] == "lookup_kb"
    assert events[0]["span_id"] == events[1]["span_id"]  # same span, two phases


async def _async_lookup(topic: str) -> str:
    return f"async result for {topic}"


@pytest.mark.asyncio
async def test_async_function_emits_start_then_end(tmp_path: Path) -> None:
    log_path = tmp_path / "trace.jsonl"
    apv_trace.configure(log_path)
    traced = apv_trace.trace(kind="tool", name="lookup_kb")(_async_lookup)

    result = await traced("weather")

    assert result == "async result for weather"
    events = _read_events(log_path)
    assert [e["phase"] for e in events] == ["start", "end"]


def test_exception_emits_error_and_still_propagates(tmp_path: Path) -> None:
    log_path = tmp_path / "trace.jsonl"
    apv_trace.configure(log_path)

    @apv_trace.trace(kind="tool", name="flaky")
    def flaky() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        flaky()

    events = _read_events(log_path)
    assert [e["phase"] for e in events] == ["start", "error"]
    assert events[1]["meta"]["error"]["type"] == "ValueError"
    assert events[1]["meta"]["error"]["message"] == "boom"


def test_error_traceback_is_truncated(tmp_path: Path) -> None:
    """Task 13 Verification #3: traceback stays <= 4096 chars, with the
    same "...<truncated>" suffix as before the OTel rewrite. A long
    exception MESSAGE (not deep recursion -- CPython's own traceback
    formatter collapses repeated identical frames into one "[Previous line
    repeated N more times]" note, which defeats trying to blow the limit
    via recursion depth) guarantees the formatted traceback exceeds 4096
    chars regardless of frame count."""
    log_path = tmp_path / "trace.jsonl"
    apv_trace.configure(log_path)

    @apv_trace.trace(kind="tool", name="deep_flaky")
    def deep_flaky() -> None:
        raise ValueError("x" * 5000)

    with pytest.raises(ValueError):
        deep_flaky()

    events = _read_events(log_path)
    error_event = next(e for e in events if e["phase"] == "error")
    traceback_text = error_event["meta"]["error"]["traceback"]
    # Task 13's own Verification #3 wording: "<= 4096 chars plus suffix" --
    # `_truncate` appends the suffix AFTER slicing to the limit, so the
    # total is the limit plus the suffix's own length, not capped at 4096.
    suffix = "...<truncated>"
    assert len(traceback_text) <= 4096 + len(suffix)
    assert traceback_text.endswith(suffix)


def test_nested_spans_chain_parent_span_id(tmp_path: Path) -> None:
    """Task 13 Verification #1: outer -> inner produces exactly
    start,start,end,end, in that order, and inner's parent is outer."""
    log_path = tmp_path / "trace.jsonl"
    apv_trace.configure(log_path)

    @apv_trace.trace(kind="tool", name="inner")
    def inner() -> None:
        pass

    @apv_trace.trace(kind="agent", name="outer")
    def outer() -> None:
        inner()

    outer()

    events = _read_events(log_path)
    assert [(e["ref"]["name"], e["phase"]) for e in events] == [
        ("outer", "start"),
        ("inner", "start"),
        ("inner", "end"),
        ("outer", "end"),
    ]
    outer_start, inner_start = events[0], events[1]
    assert outer_start["parent_span_id"] is None
    assert inner_start["parent_span_id"] == outer_start["span_id"]


def test_name_parameter_overrides_ref_name_at_call_time(tmp_path: Path) -> None:
    """The `simulate_agent(name, ...)` shape this feature exists for."""
    log_path = tmp_path / "trace.jsonl"
    apv_trace.configure(log_path)

    @apv_trace.trace(kind="agent")
    def simulate_agent(name: str, note: str = "") -> None:
        del note

    simulate_agent("Research Agent")
    simulate_agent(name="Writer Agent")

    events = _read_events(log_path)
    names = [e["ref"]["name"] for e in events if e["phase"] == "start"]
    assert names == ["Research Agent", "Writer Agent"]


def test_explicit_decorator_name_wins_over_name_parameter(tmp_path: Path) -> None:
    log_path = tmp_path / "trace.jsonl"
    apv_trace.configure(log_path)

    @apv_trace.trace(kind="agent", name="fixed_name")
    def simulate_agent(name: str) -> None:
        del name

    simulate_agent("ignored_at_runtime")

    events = _read_events(log_path)
    assert events[0]["ref"]["name"] == "fixed_name"


def test_function_without_name_parameter_falls_back_to_dunder_name(tmp_path: Path) -> None:
    log_path = tmp_path / "trace.jsonl"
    apv_trace.configure(log_path)

    @apv_trace.trace(kind="function")
    def normalize_text(text: str) -> str:
        return text.strip()

    normalize_text("  hi  ")

    events = _read_events(log_path)
    assert events[0]["ref"]["name"] == "normalize_text"


def test_manifest_populates_db_id_and_framework_id(tmp_path: Path) -> None:
    log_path = tmp_path / "trace.jsonl"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"agent:Research Agent": {"db_id": "400000000000000000000001", "framework_id": "fw-1"}})
    )
    apv_trace.configure(log_path, manifest_path=manifest_path)

    @apv_trace.trace(kind="agent")
    def simulate_agent(name: str) -> None:
        pass

    simulate_agent("Research Agent")

    events = _read_events(log_path)
    assert events[0]["ref"]["db_id"] == "400000000000000000000001"
    assert events[0]["ref"]["framework_id"] == "fw-1"


def test_manifest_miss_leaves_db_id_none(tmp_path: Path) -> None:
    log_path = tmp_path / "trace.jsonl"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"agent:Someone Else": {"db_id": "x"}}))
    apv_trace.configure(log_path, manifest_path=manifest_path)

    @apv_trace.trace(kind="agent")
    def simulate_agent(name: str) -> None:
        pass

    simulate_agent("Research Agent")

    events = _read_events(log_path)
    assert events[0]["ref"]["db_id"] is None
    assert events[0]["ref"]["framework_id"] is None


def test_missing_manifest_file_does_not_raise(tmp_path: Path) -> None:
    log_path = tmp_path / "trace.jsonl"
    apv_trace.configure(log_path, manifest_path=tmp_path / "does_not_exist.json")

    @apv_trace.trace(kind="tool", name="lookup_kb")
    def lookup_kb() -> None:
        pass

    lookup_kb()  # must not raise

    events = _read_events(log_path)
    assert events[0]["ref"]["db_id"] is None


def test_malformed_manifest_json_does_not_raise(tmp_path: Path) -> None:
    log_path = tmp_path / "trace.jsonl"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{not valid json")
    apv_trace.configure(log_path, manifest_path=manifest_path)  # must not raise

    @apv_trace.trace(kind="tool", name="lookup_kb")
    def lookup_kb() -> None:
        pass

    lookup_kb()

    events = _read_events(log_path)
    assert events[0]["ref"]["db_id"] is None


def test_malformed_manifest_entry_does_not_raise(tmp_path: Path) -> None:
    """Outer object is valid JSON, but one entry's VALUE is a string, not an
    object (e.g. a hand-edited manifest). `_ref_for` does `entry.get(...)` on
    whatever `_load_manifest` hands it, and that call happens BEFORE
    `start_as_current_span` is ever entered (building the attributes dict
    IS the call that would raise) -- so this must be filtered out at load
    time, not merely tolerated downstream."""
    log_path = tmp_path / "trace.jsonl"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"agent:Coordinator": "not-a-dict"}))
    apv_trace.configure(log_path, manifest_path=manifest_path)  # must not raise

    @apv_trace.trace(kind="agent")
    def simulate_agent(name: str) -> None:
        pass

    simulate_agent("Coordinator")  # must not raise

    events = _read_events(log_path)
    assert events[0]["ref"]["db_id"] is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fd counting via /proc/self/fd is POSIX-only; the reconfigure "
    "close itself still runs on Windows, this test just can't observe the "
    "leak there without a platform-specific API (e.g. psutil).",
)
def test_configure_twice_closes_the_previous_file_handle(tmp_path: Path) -> None:
    """Regression test (pre-dates the OTel rewrite, still applies): a stale
    `_JsonlSpanProcessor` left registered on the shared `TracerProvider`
    after `configure()` is called twice must have its OWN file handle
    closed via `shutdown()`, or every reconfigure (a long-running target
    project rotating its log path, or a test suite calling `configure()`
    per test) leaks one more fd forever."""
    fd_dir = Path(f"/proc/{os.getpid()}/fd")
    fd_count_before = len(os.listdir(fd_dir))

    apv_trace.configure(tmp_path / "first.jsonl")
    apv_trace.configure(tmp_path / "second.jsonl")

    fd_count_after = len(os.listdir(fd_dir))
    assert fd_count_after <= fd_count_before + 1  # at most the ONE current handle


def test_trace_before_configure_is_inert_not_fatal(tmp_path: Path) -> None:
    """Task 13 Verification #7. No real `TracerProvider` is set yet --
    `trace.get_tracer()` returns a `ProxyTracer` over a no-op provider, so
    the decorated function must still run and return normally, and crucially
    no log file gets created (there's nowhere configured for one to go, and
    nothing should create one speculatively)."""
    log_path = tmp_path / "trace.jsonl"  # never passed to configure()

    @apv_trace.trace(kind="tool", name="lookup_kb")
    def lookup_kb() -> str:
        return "ok"

    assert lookup_kb() == "ok"
    assert not log_path.exists()


@pytest.mark.asyncio
async def test_async_concurrent_calls_each_have_the_caller_as_parent(tmp_path: Path) -> None:
    """Task 13 Verification #2: two @trace coroutines run concurrently
    under asyncio.gather, each awaiting a real suspension point. Each
    child's parent must be the CALLER span, never its sibling -- OTel's
    contextvars-based context is copied per-asyncio-task (same guarantee
    the hand-rolled `_current_span` ContextVar gave before this rewrite),
    so concurrent children don't cross-contaminate parent_span_id."""
    log_path = tmp_path / "trace.jsonl"
    apv_trace.configure(log_path)

    @apv_trace.trace(kind="tool", name="child")
    async def child(delay: float) -> None:
        await asyncio.sleep(delay)

    @apv_trace.trace(kind="agent", name="caller")
    async def caller() -> None:
        await asyncio.gather(child(0.01), child(0.02))

    await caller()

    events = _read_events(log_path)
    caller_start = next(e for e in events if e["ref"]["name"] == "caller" and e["phase"] == "start")
    child_starts = [e for e in events if e["ref"]["name"] == "child" and e["phase"] == "start"]
    assert len(child_starts) == 2
    for child_start in child_starts:
        assert child_start["parent_span_id"] == caller_start["span_id"]
    # Never each other's parent -- the regression this test exists for.
    child_span_ids = {e["span_id"] for e in child_starts}
    child_parent_ids = {e["parent_span_id"] for e in child_starts}
    assert not (child_span_ids & child_parent_ids)


def test_start_line_is_on_disk_while_the_traced_call_is_still_running(tmp_path: Path) -> None:
    """Task 13 Verification #4: `on_start` fires synchronously when the
    span begins, so the start line is already readable from disk WHILE the
    traced call is still running -- not just after it returns. Guards the
    Design's own Risk note ("on_start sees attributes only if they were
    passed at span start -- easy to regress")."""
    log_path = tmp_path / "trace.jsonl"
    apv_trace.configure(log_path)
    seen_during_call: list[dict] = []

    @apv_trace.trace(kind="tool", name="mid_call_check")
    def traced() -> None:
        seen_during_call.extend(_read_events(log_path))

    traced()

    assert len(seen_during_call) == 1
    assert seen_during_call[0]["phase"] == "start"
    assert seen_during_call[0]["ref"]["name"] == "mid_call_check"


def test_span_without_apv_kind_is_skipped_and_logged_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Task 14's own Risk list: spans without `apv.kind` (e.g. a target
    project's own HTTP/DB auto-instrumentation sharing the same
    `TracerProvider`) are expected to be silently skipped from the JSONL --
    but "silently" to the FILE, not invisibly: a DEBUG-level log line
    should still exist for anyone investigating why a span didn't show up.
    Starts a span directly through `_state.tracer` (not `@trace`, which
    always sets `apv.kind`) to simulate exactly that "someone else's span
    on our shared provider" shape.
    """
    log_path = tmp_path / "trace.jsonl"
    apv_trace.configure(log_path)

    with (
        caplog.at_level("DEBUG", logger="apv_trace"),
        apv_trace._state.tracer.start_as_current_span("unrelated_http_call"),
    ):
        pass

    assert _read_events(log_path) == []  # not written to the JSONL at all
    assert any(
        "unrelated_http_call" in record.message and "apv.kind" in record.message
        for record in caplog.records
    )


def test_repeated_configure_each_event_appears_exactly_once(tmp_path: Path) -> None:
    """Task 13 Verification #8. Two `configure()` calls targeting DIFFERENT
    files in the same process must not duplicate events: the first
    processor is shut down (becomes a permanent no-op) before the second is
    registered on the shared `TracerProvider`, so a traced call issued
    AFTER the second `configure()` lands in the second file only -- never
    both, never twice."""
    first_log = tmp_path / "first.jsonl"
    second_log = tmp_path / "second.jsonl"
    apv_trace.configure(first_log)

    @apv_trace.trace(kind="tool", name="shared_name")
    def traced() -> None:
        pass

    traced()  # -> first_log only

    apv_trace.configure(second_log)
    traced()  # -> second_log only; must NOT also append to first_log

    first_events = _read_events(first_log)
    second_events = _read_events(second_log)
    assert len(first_events) == 2  # one call (start+end), from before reconfigure
    assert len(second_events) == 2  # one call (start+end), from after reconfigure


def test_processor_never_raises_after_its_file_handle_is_closed(tmp_path: Path) -> None:
    """Task 13 Verification #9. Simulates the file handle dying out from
    under the processor (disk full, something external closing it) --
    distinct from a graceful `shutdown()` (which also sets `_closed=True`).
    `on_start`/`on_end`'s own try/except must catch the resulting write
    failure; the traced function must still run and return normally, same
    "instrumentation must never crash the host project" guarantee as every
    other failure path in this module."""
    log_path = tmp_path / "trace.jsonl"
    apv_trace.configure(log_path)
    assert apv_trace._state.processor is not None
    apv_trace._state.processor._file.close()  # simulate the handle dying

    @apv_trace.trace(kind="tool", name="lookup_kb")
    def lookup_kb() -> str:
        return "ok"

    assert lookup_kb() == "ok"  # must not raise despite the write failing


def test_kill_mid_run_leaves_a_start_with_no_matching_end(tmp_path: Path) -> None:
    """Task 13 Verification #10. A process killed mid-span must leave a
    `start` line on disk with no matching `end` -- proof writes are
    genuinely unbuffered/synchronous: `on_start` fires (and its OS write()
    call completes) before the span's body starts sleeping, so the line is
    already durable on disk by the time the kill arrives."""
    log_path = tmp_path / "trace.jsonl"
    script = (
        "import time, apv_trace\n"
        "from pathlib import Path\n"
        f"apv_trace.configure(Path({str(log_path)!r}))\n"
        "@apv_trace.trace(kind='tool', name='slow_tool')\n"
        "def slow_tool():\n"
        "    time.sleep(30)\n"
        "slow_tool()\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if log_path.exists() and log_path.read_text().strip():
                break
            time.sleep(0.05)
        else:
            pytest.fail("subprocess never wrote a start line within 10s")
    finally:
        proc.kill()
        proc.wait(timeout=10)

    events = _read_events(log_path)
    assert len(events) == 1
    assert events[0]["phase"] == "start"
    assert events[0]["ref"]["name"] == "slow_tool"


def test_configure_defaults_to_the_shared_live_log_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 21 D1: with no `log_path` argument and no `APV_TRACE_LOG_PATH`,
    `configure()` must resolve to the SAME shared, stable path
    `default_log_path()` returns -- not a fresh `<run_id>.jsonl` file every
    run, which is what let the backend and the target disagree on where
    to look by default."""
    monkeypatch.delenv("APV_TRACE_LOG_PATH", raising=False)
    # Isolates the real user log dir from this test run -- `configure()`
    # creates the resolved path's parent directory and opens it for append.
    monkeypatch.setattr(apv_trace.platformdirs, "user_log_dir", lambda name: str(tmp_path / name))

    resolved = apv_trace.configure()

    assert resolved == apv_trace.default_log_path()
    assert resolved.name == "live.jsonl"


def test_apv_trace_log_path_env_var_still_overrides_the_shared_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "custom.jsonl"
    monkeypatch.setenv("APV_TRACE_LOG_PATH", str(override))

    resolved = apv_trace.configure()

    assert resolved == override
