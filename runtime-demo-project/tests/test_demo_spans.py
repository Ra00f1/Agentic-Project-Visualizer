"""Tests for the runtime demo's raw-OpenTelemetry instrumentation (task 14).

`runtime_demo/` used to decorate its calls with `apv_trace.trace(kind=...)`;
now each call wraps itself in an explicit `opentelemetry.trace.get_tracer(
__name__).start_as_current_span(...)` with a hand-written `apv.*`
attributes dict (see each module's own `_*_ATTRS` constant). These tests
guard the two ways that can silently go wrong — a stale `apv.qualname`
after a rename, or a typo in `apv.kind` — plus the demo's own contract: one
run exercises every kind, the CRASH ticket's error path fires exactly
where expected, and span nesting survives the `async`/`await` boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from runtime_demo import agent, functions, tools, workflow

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_qualnames_match_the_real_functions() -> None:
    """Each hand-written `apv.qualname` must equal the real `fn.__qualname__` — the only guard against it drifting on a rename."""
    checks = [
        (functions.normalize_text, functions._NORMALIZE_TEXT_ATTRS),
        (tools.classify_ticket_tool, tools._CLASSIFY_TICKET_ATTRS),
        (tools.lookup_kb_tool, tools._LOOKUP_KB_ATTRS),
        (agent.TriageAgent.handle, agent._HANDLE_ATTRS),
        (workflow.run_support_ticket_workflow, workflow._WORKFLOW_ATTRS),
    ]
    for fn, attrs in checks:
        assert fn.__qualname__ == attrs["apv.qualname"], fn


def _run_demo(tmp_path: Path) -> list[dict[str, Any]]:
    """Run `run_demo` as a subprocess, pointed at a scratch log file, and parse every line it wrote.

    Subprocess, not an in-process call: `apv_trace.configure()` touches
    OpenTelemetry's process-wide `TracerProvider`, which only accepts
    `set_tracer_provider()` once per process — a fresh subprocess per test
    is a clean slate for free, same reasoning as
    `apps/backend/tests/trace/test_apv_trace_contract.py`.
    """
    log_path = tmp_path / "trace.jsonl"
    env = {**os.environ, "APV_TRACE_LOG_PATH": str(log_path)}
    result = subprocess.run(
        [sys.executable, "-m", "runtime_demo.run_demo"],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert lines, "run_demo wrote no trace events"
    return [json.loads(line) for line in lines]


def test_all_four_kinds_are_present(tmp_path: Path) -> None:
    """A typo in any `apv.kind` would silently drop that kind's spans from the log instead of raising."""
    events = _run_demo(tmp_path)
    kinds = {event["kind"] for event in events}
    assert kinds == {"workflow", "agent", "tool", "function"}


def test_crash_ticket_produces_exactly_one_agent_error(tmp_path: Path) -> None:
    events = _run_demo(tmp_path)
    agent_errors = [e for e in events if e["kind"] == "agent" and e["phase"] == "error"]
    assert len(agent_errors) == 1
    assert agent_errors[0]["meta"]["error"]["type"] == "RuntimeError"


def test_every_start_has_a_matching_end_or_error(tmp_path: Path) -> None:
    """The CRASH ticket's spans end in `error`, not `end` — both count as the span having finished."""
    events = _run_demo(tmp_path)
    started = {e["span_id"] for e in events if e["phase"] == "start"}
    finished = {e["span_id"] for e in events if e["phase"] in ("end", "error")}
    assert started == finished


def test_tool_spans_are_children_of_the_agent_handle_span(tmp_path: Path) -> None:
    """Proves span nesting survives `TriageAgent.handle`'s `await` boundary down to the two tool calls it makes directly."""
    events = _run_demo(tmp_path)
    handle_span_ids = {
        e["span_id"]
        for e in events
        if e["ref"]["qualname"] == "TriageAgent.handle" and e["phase"] == "start"
    }
    tool_starts = [
        e for e in events if e["ref"]["name"] in ("classify_ticket", "lookup_kb") and e["phase"] == "start"
    ]
    assert len(tool_starts) == 6  # 2 tools x 3 successful tickets (CRASH never reaches them)
    for event in tool_starts:
        assert event["parent_span_id"] in handle_span_ids
