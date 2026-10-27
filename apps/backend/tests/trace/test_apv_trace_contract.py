"""Contract test: `apv_trace`'s REAL emitter output, parsed by the REAL
`TraceEvent` model.

`test_events.py` validates a hand-copied example dict kept in sync with the
emitter BY HAND (its own docstring says so). This file is the independent
check that the sync hasn't drifted: it runs the actual `apv_trace` package
(a dev-only dependency here — never a runtime one, see CLAUDE.md §1) in a
subprocess, doing nested sync calls, concurrent async calls, one error, and
(task 17) three synthetic OpenInference spans exercising both of
`_JsonlSpanProcessor`'s classification paths, then parses every line it
wrote with the app's own `TraceEvent.model_validate_json` — the exact call
`FileTailTraceSource` makes per line. Task 13's own Design note: "Two
independent implementations agreeing on a shape is exactly what a JSON
Schema contract is for."

Subprocess, not an in-process import: `apv_trace` keeps process-wide OTel/
emitter state (deliberately — see its own module docstring), and this repo
already carries a test-isolation story for that inside `packages/apv_trace`
itself. Importing it directly into this app's test process would be a
second copy of that problem for a cross-package contract test that doesn't
need it — a fresh subprocess is a clean slate for free.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic_visualizer.domain import Edge, Graph, Node, Provenance
from agentic_visualizer.runtime.node_index import NodeIndex, Resolved, Unresolved
from agentic_visualizer.trace.events import TraceEvent

# Three TOP-LEVEL (root) calls, each with at least one level of nesting or
# concurrency beneath it, plus one that raises. Kept as a module constant
# (not an f-string built inline in the test) so the "which names are roots"
# list below it stays obviously in sync with what this script actually calls
# at module level.
_SCRIPT = """
import asyncio
from pathlib import Path
import apv_trace

apv_trace.configure(Path({log_path!r}))


@apv_trace.trace(kind="tool", name="inner_tool")
def inner_tool():
    pass


@apv_trace.trace(kind="agent", name="outer_agent")
def outer_sync():
    inner_tool()


@apv_trace.trace(kind="tool", name="async_child")
async def async_child(delay):
    await asyncio.sleep(delay)


@apv_trace.trace(kind="agent", name="async_caller")
async def async_caller():
    await asyncio.gather(async_child(0.001), async_child(0.002))


@apv_trace.trace(kind="tool", name="flaky_tool")
def flaky():
    raise RuntimeError("contract test failure")


outer_sync()
asyncio.run(async_caller())
try:
    flaky()
except RuntimeError:
    pass

# Task 17: synthetic OpenInference spans, exercising both classification
# paths through the SAME global provider `apv_trace.configure()` just set
# up -- no `apv.*` attributes anywhere below, only `openinference.span.kind`
# and the name attributes a real instrumentor would set.
from opentelemetry import trace as otel_trace  # noqa: E402

_oi_tracer = otel_trace.get_tracer("openinference.instrumentation.test")

# Live TOOL: kind + name attribute both present at span creation.
with _oi_tracer.start_as_current_span(
    "oi_live_tool", attributes={{"openinference.span.kind": "TOOL", "tool.name": "oi_live_tool"}}
):
    pass

# Deferred TOOL: no attributes at start (LangChain's own style) -- kind and
# name only set just before the span ends.
with _oi_tracer.start_as_current_span("oi_deferred_tool") as oi_span:
    oi_span.set_attribute("openinference.span.kind", "TOOL")
    oi_span.set_attribute("tool.name", "oi_deferred_tool")

# Deferred LLM: kind present at start, but its name attribute isn't set
# until later -- not start-eligible, so this defers too.
with _oi_tracer.start_as_current_span(
    "oi_deferred_llm", attributes={{"openinference.span.kind": "LLM"}}
) as oi_span:
    oi_span.set_attribute("llm.request.model_name", "oi-deferred-model")
"""

_ROOT_REF_NAMES = {
    "outer_agent",
    "async_caller",
    "flaky_tool",
    "oi_live_tool",
    "oi_deferred_tool",
    "oi-deferred-model",
}


def test_real_emitter_output_parses_with_trace_event(tmp_path: Path) -> None:
    log_path = tmp_path / "trace.jsonl"
    script = _SCRIPT.format(log_path=str(log_path))

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert lines, "subprocess wrote no trace events"

    # The actual contract check: every line the REAL emitter wrote must
    # parse with the REAL TraceEvent model. A ValidationError here means
    # the two sides of the JSONL contract have drifted.
    events = [TraceEvent.model_validate_json(line) for line in lines]

    error_events = [e for e in events if e.phase == "error"]
    assert error_events, "expected at least one error event (flaky_tool raises)"
    for error_event in error_events:
        error_meta = error_event.meta.get("error")
        assert error_meta is not None, error_event
        assert error_meta["type"] == "RuntimeError"

    for event in events:
        if event.ref.name not in _ROOT_REF_NAMES:
            assert event.parent_span_id is not None, (
                f"non-root span {event.ref.name!r} has no parent_span_id: {event}"
            )

    # Task 17: the three synthetic OpenInference spans, parsed by the SAME
    # real TraceEvent model as everything else above -- the actual contract
    # check for the deferred-emission path, not a synthetic one.
    oi_events = [e for e in events if e.meta.get("source") == "openinference"]
    assert {e.ref.name for e in oi_events} == {"oi_live_tool", "oi_deferred_tool", "oi-deferred-model"}
    assert {e.kind for e in oi_events} == {"tool", "model"}

    for name in ("oi_live_tool", "oi_deferred_tool", "oi-deferred-model"):
        indices = [i for i, e in enumerate(events) if e.ref.name == name]
        assert [events[i].phase for i in indices] == ["start", "end"]
        # Deferred spans (oi_deferred_tool, oi-deferred-model) write their
        # start line immediately before their end line, in FILE order --
        # this is what distinguishes "classified late but still correctly
        # ordered" from "classified late and out of order."
        assert indices[0] < indices[1]


# Verification 17: a minimal replay script -- one synthetic live TOOL span
# named "add" and one deferred LLM span with a dated model id, nothing
# else. Kept separate from `_SCRIPT` above: this one feeds a hand-made
# `Graph` with exactly the two matching nodes, and a dead letter from an
# unrelated root span in the bigger script would be noise here, not signal.
_REPLAY_SCRIPT = """
from pathlib import Path
import apv_trace
from opentelemetry import trace as otel_trace

apv_trace.configure(Path({log_path!r}))
tracer = otel_trace.get_tracer("openinference.instrumentation.test")

with tracer.start_as_current_span(
    "add", attributes={{"openinference.span.kind": "TOOL", "tool.name": "add"}}
):
    pass

with tracer.start_as_current_span(
    "ChatAnthropic", attributes={{"openinference.span.kind": "LLM"}}
) as span:
    span.set_attribute("llm.request.model_name", "claude-sonnet-4-20250514")
"""


def _prov() -> Provenance:
    return Provenance(source="test", source_ref="test:1", scanned_at=datetime(2026, 1, 1, tzinfo=UTC))


def test_replay_resolves_synthetic_openinference_events_with_zero_dead_letters(tmp_path: Path) -> None:
    """Verification 17: feed the real emitter's OpenInference-derived
    events into a `NodeIndex` built from a hand-made graph with exactly
    the two nodes they should resolve to. Both resolve; nothing dead-letters."""
    log_path = tmp_path / "trace.jsonl"
    script = _REPLAY_SCRIPT.format(log_path=str(log_path))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    events = [TraceEvent.model_validate_json(line) for line in lines]
    assert events, "replay script wrote no trace events"

    graph = Graph(
        nodes=[
            Node(id="tool:add", type="tool", name="add", provenance=_prov(), attributes={}),
            Node(id="model:claude-sonnet-4", type="model", name="claude-sonnet-4", provenance=_prov(), attributes={}),
        ]
    )
    index = NodeIndex.build(graph)

    results = [index.resolve(event) for event in events]
    assert all(isinstance(r, Resolved) for r in results), results
    tool_results = [r for e, r in zip(events, results, strict=True) if e.kind == "tool"]
    model_results = [r for e, r in zip(events, results, strict=True) if e.kind == "model"]
    assert tool_results and all(r.node_ids == ["tool:add"] for r in tool_results)  # type: ignore[union-attr]
    assert model_results and all(r.node_ids == ["model:claude-sonnet-4"] for r in model_results)  # type: ignore[union-attr]


# Task 18 Verification 25: the plain, un-traced fixture project `apv-run
# --functions` is meant to prove zero-code tracing against. Hand-built
# function nodes (not a live L3 scan) with `file_path`/`qualname`
# attributes shaped exactly as L2/L3 would produce them -- this test is
# about NodeIndex resolution of REAL emitted events, not scanner
# correctness, which has its own dedicated tests elsewhere.
_PLAIN_PROJECT_ROOT = Path(__file__).resolve().parents[4] / "packages" / "apv_trace" / "tests" / "fixtures" / "plain_project"


def _plain_fn_node(node_id: str, name: str, file_path: str, qualname: str) -> Node:
    return Node(
        id=node_id,
        type="function",
        name=name,
        provenance=_prov(),
        attributes={"file_path": file_path, "qualname": qualname},
    )


def test_replay_apv_run_functions_against_the_plain_fixture(tmp_path: Path) -> None:
    """Verification 25. Runs the REAL `apv-run --functions` launcher
    against `packages/apv_trace/tests/fixtures/plain_project` (no tracing
    code anywhere in that fixture) via a 3.12+ interpreter resolved
    through `uv` -- this app's own dev venv may be on an older Python,
    same reason `FunctionTracer`'s own tests are version-gated."""
    if not _PLAIN_PROJECT_ROOT.is_dir():
        pytest.skip(f"plain_project fixture not found at {_PLAIN_PROJECT_ROOT}")

    log_path = tmp_path / "trace.jsonl"
    apv_trace_dir = _PLAIN_PROJECT_ROOT.parents[1]  # packages/apv_trace

    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(apv_trace_dir),
            "--python",
            "3.12",
            "python",
            "-m",
            "apv_trace.run",
            "--log",
            str(log_path),
            "--no-openinference",
            "--functions",
            ".",
            "--",
            "python",
            "-m",
            "plain_app.main",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_PLAIN_PROJECT_ROOT),
    )
    assert result.returncode == 0, result.stderr

    events = [TraceEvent.model_validate_json(line) for line in log_path.read_text().splitlines() if line]
    assert events, "apv-run wrote no trace events"

    graph = Graph(
        nodes=[
            _plain_fn_node("fn:main", "main", "plain_app/main.py", "main"),
            _plain_fn_node("fn:handle", "handle", "plain_app/agent.py", "handle"),
            _plain_fn_node("fn:handle_async", "handle_async", "plain_app/agent.py", "handle_async"),
            _plain_fn_node("fn:handle_and_catch", "handle_and_catch", "plain_app/agent.py", "handle_and_catch"),
            _plain_fn_node("fn:handle_and_raise", "handle_and_raise", "plain_app/agent.py", "handle_and_raise"),
            _plain_fn_node("fn:classify", "classify", "plain_app/tools.py", "classify"),
            _plain_fn_node("fn:lookup", "lookup", "plain_app/tools.py", "lookup"),
            Node(id="tool:lookup_kb", type="tool", name="lookup_kb", provenance=_prov(), attributes={}),
        ],
        edges=[Edge(source_id="tool:lookup_kb", target_id="fn:lookup", kind="implements", provenance=_prov())],
    )
    index = NodeIndex.build(graph)

    results = [(event, index.resolve(event)) for event in events]
    dead_letters = [(event.ref.qualname, result) for event, result in results if isinstance(result, Unresolved)]
    assert dead_letters == []

    lookup_results = [result for event, result in results if event.ref.qualname == "lookup"]
    assert lookup_results
    for lookup_result in lookup_results:
        assert isinstance(lookup_result, Resolved)
        assert set(lookup_result.node_ids) == {"fn:lookup", "tool:lookup_kb"}
        assert lookup_result.strategy == "module_qualname+bridge"
