"""Tests for `FunctionTracer` (task 18, D2) — `sys.monitoring`-based
function tracing. Skipped entirely below Python 3.12.

Every test loads a small, throwaway fixture MODULE from `tmp_path` (never
functions defined inline in this test file) and points `root=tmp_path` at
it. That keeps each test's traced code fully isolated from pytest's own
internals and from every other test module in this package — nothing
here risks tracing pytest's assertion-rewriting machinery or a sibling
test file by accident.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring requires Python 3.12+")

if sys.version_info >= (3, 12):
    from apv_trace._monitor import FunctionTracer, module_from_path


def _load_module(tmp_path: Path, name: str, source: str) -> ModuleType:
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tracer_factory() -> Any:
    created: list[FunctionTracer] = []

    def factory(**kwargs: Any) -> FunctionTracer:
        tracer = FunctionTracer(**kwargs)
        created.append(tracer)
        return tracer

    yield factory
    for tracer in created:
        tracer.stop()


def _names(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(e["ref"]["qualname"], e["phase"]) for e in events]


def test_sync_nesting_has_the_correct_parent_chain(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 11."""
    module = _load_module(
        tmp_path,
        "sync_nest",
        "def outer():\n    return inner() + 1\n\n\ndef inner():\n    return 1\n",
    )
    events: list[dict[str, Any]] = []
    tracer = tracer_factory(root=tmp_path, write=events.append, run_id="r1", max_calls=10_000, bridge=True, warn=print)
    tracer.start()
    module.outer()
    tracer.stop()

    assert _names(events) == [("outer", "start"), ("inner", "start"), ("inner", "end"), ("outer", "end")]
    outer_start, inner_start = events[0], events[1]
    assert outer_start["parent_span_id"] is None
    assert inner_start["parent_span_id"] == outer_start["span_id"]


def test_directly_awaited_coroutine_is_parented_to_its_caller(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 12a."""
    module = _load_module(
        tmp_path,
        "coro_direct",
        "async def caller():\n    return await callee()\n\n\nasync def callee():\n    return 1\n",
    )
    events: list[dict[str, Any]] = []
    tracer = tracer_factory(root=tmp_path, write=events.append, run_id="r1", max_calls=10_000, bridge=True, warn=print)
    tracer.start()
    asyncio.run(module.caller())
    tracer.stop()

    caller_start = next(e for e in events if e["ref"]["qualname"] == "caller" and e["phase"] == "start")
    callee_start = next(e for e in events if e["ref"]["qualname"] == "callee" and e["phase"] == "start")
    assert callee_start["parent_span_id"] == caller_start["span_id"]


def test_gather_run_coroutine_is_a_root_span(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 12b: documented behavior, not a bug -- a coroutine run
    as a separate Task via `gather` has the event loop's own frames on
    `f_back`, not the caller's."""
    module = _load_module(
        tmp_path,
        "coro_gather",
        "import asyncio\n\n\nasync def caller():\n    await asyncio.gather(callee())\n\n\nasync def callee():\n    return 1\n",
    )
    events: list[dict[str, Any]] = []
    tracer = tracer_factory(root=tmp_path, write=events.append, run_id="r1", max_calls=10_000, bridge=True, warn=print)
    tracer.start()
    asyncio.run(module.caller())
    tracer.stop()

    callee_start = next(e for e in events if e["ref"]["qualname"] == "callee" and e["phase"] == "start")
    assert callee_start["parent_span_id"] is None


def test_generator_is_one_span_from_first_next_to_exhaustion(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 13a."""
    module = _load_module(tmp_path, "gen_exhaust", "def gen():\n    yield 1\n    yield 2\n")
    events: list[dict[str, Any]] = []
    tracer = tracer_factory(root=tmp_path, write=events.append, run_id="r1", max_calls=10_000, bridge=True, warn=print)
    tracer.start()
    list(module.gen())
    tracer.stop()

    gen_events = [e for e in events if e["ref"]["qualname"] == "gen"]
    assert [e["phase"] for e in gen_events] == ["start", "end"]
    assert gen_events[0]["span_id"] == gen_events[1]["span_id"]


def test_abandoned_generator_closed_writes_end_with_generator_exit(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 13b.

    The generator needs a `try/finally` around its `yield`s. Confirmed
    empirically (not assumed): a BARE generator (no exception-handling
    block at all around its yields) closed via `.close()` fires
    `PY_UNWIND` on Python 3.12 but not on 3.13 -- CPython can apparently
    tear down that frame without invoking the instrumented unwind path
    when there's no exception-table entry to consult. Real generators
    overwhelmingly have cleanup code (a `finally`, or a context manager
    wrapping their body), which restores the event reliably on both
    versions -- documented as a narrower, version-dependent gap in
    `packages/apv_trace/README.md`'s "Known limitations" for the bare
    case specifically.
    """
    module = _load_module(
        tmp_path, "gen_abandon", "def gen():\n    try:\n        yield 1\n        yield 2\n    finally:\n        pass\n"
    )
    events: list[dict[str, Any]] = []
    tracer = tracer_factory(root=tmp_path, write=events.append, run_id="r1", max_calls=10_000, bridge=True, warn=print)
    tracer.start()
    g = module.gen()
    next(g)
    g.close()
    tracer.stop()

    gen_events = [e for e in events if e["ref"]["qualname"] == "gen"]
    assert [e["phase"] for e in gen_events] == ["start", "end"]
    assert gen_events[1]["meta"]["unwind"] == "GeneratorExit"


def test_exception_through_two_frames_writes_two_error_lines(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 14a."""
    module = _load_module(
        tmp_path,
        "exc_two_frames",
        "def outer():\n    inner()\n\n\ndef inner():\n    raise ValueError('boom')\n",
    )
    events: list[dict[str, Any]] = []
    tracer = tracer_factory(root=tmp_path, write=events.append, run_id="r1", max_calls=10_000, bridge=True, warn=print)
    tracer.start()
    with pytest.raises(ValueError, match="boom"):
        module.outer()
    tracer.stop()

    assert _names(events) == [("outer", "start"), ("inner", "start"), ("inner", "error"), ("outer", "error")]
    for event in events:
        if event["phase"] == "error":
            assert event["meta"]["error"]["type"] == "ValueError"


def test_caught_exception_callee_errors_caller_ends(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 14b."""
    module = _load_module(
        tmp_path,
        "exc_caught",
        "def outer():\n    try:\n        inner()\n    except ValueError:\n        return 'recovered'\n\n\ndef inner():\n    raise ValueError('boom')\n",
    )
    events: list[dict[str, Any]] = []
    tracer = tracer_factory(root=tmp_path, write=events.append, run_id="r1", max_calls=10_000, bridge=True, warn=print)
    tracer.start()
    assert module.outer() == "recovered"
    tracer.stop()

    assert _names(events) == [("outer", "start"), ("inner", "start"), ("inner", "error"), ("outer", "end")]


def test_cancelled_error_writes_end_with_unwind(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 15. `asyncio.CancelledError` is matched by class NAME
    (not `isinstance`), so a plain sync raise exercises the exact same
    code path real task cancellation would."""
    module = _load_module(
        tmp_path,
        "cancel_mod",
        "import asyncio\n\n\ndef cancellable():\n    raise asyncio.CancelledError()\n",
    )
    events: list[dict[str, Any]] = []
    tracer = tracer_factory(root=tmp_path, write=events.append, run_id="r1", max_calls=10_000, bridge=True, warn=print)
    tracer.start()
    with pytest.raises(asyncio.CancelledError):
        module.cancellable()
    tracer.stop()

    cancel_events = [e for e in events if e["ref"]["qualname"] == "cancellable"]
    assert [e["phase"] for e in cancel_events] == ["start", "end"]
    assert cancel_events[1]["meta"]["unwind"] == "CancelledError"


def test_filters_exclude_module_level_lambdas_genexprs_locals_and_venv(
    tmp_path: Path, tracer_factory: Any
) -> None:
    """Verification 16."""
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "vendored.py").write_text("def vendored_fn():\n    return 1\n")
    module = _load_module(
        tmp_path,
        "filters_mod",
        "import sys\n"
        "sys.path.insert(0, __file__.rsplit(chr(92), 1)[0] + chr(92) + '.venv' + chr(92) + 'lib')\n"
        "import vendored\n"
        "\n"
        "f = lambda: 1\n"
        "\n"
        "def uses_genexpr():\n"
        "    return sum(x for x in range(3))\n"
        "\n"
        "def has_nested():\n"
        "    def nested():\n"
        "        return 1\n"
        "    return nested()\n"
        "\n"
        "def uses_vendored():\n"
        "    return vendored.vendored_fn()\n",
    )
    events: list[dict[str, Any]] = []
    tracer = tracer_factory(root=tmp_path, write=events.append, run_id="r1", max_calls=10_000, bridge=True, warn=print)
    tracer.start()
    module.f()
    module.uses_genexpr()
    module.has_nested()
    module.uses_vendored()
    tracer.stop()

    qualnames = {e["ref"]["qualname"] for e in events}
    assert qualnames == {"uses_genexpr", "has_nested", "uses_vendored"}


def test_apv_trace_own_frames_are_never_traced(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 16 (continued): a project function calling INTO
    `apv_trace` itself must not generate a function event for that call."""
    module = _load_module(
        tmp_path,
        "calls_apv_trace",
        "from apv_trace._processor import _truncate\n\n\ndef uses_apv_trace():\n    return _truncate('hello', 3)\n",
    )
    events: list[dict[str, Any]] = []
    tracer = tracer_factory(root=tmp_path, write=events.append, run_id="r1", max_calls=10_000, bridge=True, warn=print)
    tracer.start()
    module.uses_apv_trace()
    tracer.stop()

    qualnames = {e["ref"]["qualname"] for e in events}
    assert qualnames == {"uses_apv_trace"}


def test_cap_stops_after_max_calls_with_one_warning(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 17."""
    module = _load_module(tmp_path, "hot_fn", "def hot():\n    return 1\n")
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    tracer = tracer_factory(
        root=tmp_path, write=events.append, run_id="r1", max_calls=3, bridge=True, warn=warnings.append
    )
    tracer.start()
    for _ in range(8):
        module.hot()
    tracer.stop()

    starts = [e for e in events if e["phase"] == "start"]
    assert len(starts) == 3
    assert len(warnings) == 1
    assert "3 calls" in warnings[0]


def test_trace_decorator_dedupe_writes_no_function_lines(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 18."""
    import apv_trace

    log_path = tmp_path / "apv_trace_log.jsonl"
    apv_trace.configure(log_path)
    try:
        module = _load_module(
            tmp_path,
            "dedupe_mod",
            "from apv_trace import trace\n\n\n@trace(kind='tool', name='my_tool')\ndef my_tool_fn():\n    return 1\n",
        )
        events: list[dict[str, Any]] = []
        tracer = tracer_factory(
            root=tmp_path, write=events.append, run_id="r1", max_calls=10_000, bridge=True, warn=print
        )
        tracer.start()
        module.my_tool_fn()
        tracer.stop()

        assert events == []
        tool_lines = [json.loads(line) for line in log_path.read_text().splitlines() if line]
        assert [line["phase"] for line in tool_lines] == ["start", "end"]
        assert tool_lines[0]["kind"] == "tool"
    finally:
        if apv_trace._state.processor is not None:
            apv_trace._state.processor.shutdown()


def test_no_bridge_flag_marks_every_function_event(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 19."""
    module = _load_module(tmp_path, "bridge_mod", "def fn():\n    return 1\n")

    default_events: list[dict[str, Any]] = []
    default_tracer = tracer_factory(
        root=tmp_path, write=default_events.append, run_id="r1", max_calls=10_000, bridge=True, warn=print
    )
    default_tracer.start()
    module.fn()
    default_tracer.stop()
    assert all("bridge" not in e["meta"] for e in default_events)

    no_bridge_events: list[dict[str, Any]] = []
    no_bridge_tracer = tracer_factory(
        root=tmp_path, write=no_bridge_events.append, run_id="r2", max_calls=10_000, bridge=False, warn=print
    )
    no_bridge_tracer.start()
    module.fn()
    no_bridge_tracer.stop()
    assert all(e["meta"]["bridge"] is False for e in no_bridge_events)


def test_module_derivation_matches_node_index_convention() -> None:
    """Verification 20."""
    root = os.path.join("C:" + os.sep, "proj") if sys.platform == "win32" else "/proj"
    assert module_from_path(os.path.join(root, "pkg", "mod.py"), root) == "pkg.mod"
    assert module_from_path(os.path.join(root, "pkg", "__init__.py"), root) == "pkg.__init__"
    assert module_from_path(os.path.join(root, "script.py"), root) == "script"


def test_tool_id_exhaustion_warns_and_disables_tracing(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 21."""
    module = _load_module(tmp_path, "exhaustion_mod", "def fn():\n    return 1\n")

    for claimed_id in (2, 3, 4):
        sys.monitoring.use_tool_id(claimed_id, "test_claim")
    try:
        events: list[dict[str, Any]] = []
        warnings: list[str] = []
        tracer = tracer_factory(
            root=tmp_path, write=events.append, run_id="r1", max_calls=10_000, bridge=True, warn=warnings.append
        )
        started = tracer.start()
        module.fn()

        assert started is False
        assert len(warnings) == 1
        assert events == []
    finally:
        for claimed_id in (2, 3, 4):
            sys.monitoring.free_tool_id(claimed_id)


def test_every_event_qualname_matches_the_real_function(tmp_path: Path, tracer_factory: Any) -> None:
    """Verification 22: guards the `sys._getframe(1)` assumption `on_start`/
    `on_return`/`on_unwind` all make."""
    module = _load_module(
        tmp_path,
        "frame_guard_mod",
        "def a():\n    return b()\n\n\ndef b():\n    return c()\n\n\ndef c():\n    return 1\n",
    )
    events: list[dict[str, Any]] = []
    tracer = tracer_factory(root=tmp_path, write=events.append, run_id="r1", max_calls=10_000, bridge=True, warn=print)
    tracer.start()
    module.a()
    tracer.stop()

    real_qualnames = {"a", "b", "c"}
    for event in events:
        assert event["ref"]["qualname"] in real_qualnames
        real_fn = getattr(module, event["ref"]["qualname"])
        assert event["ref"]["qualname"] == real_fn.__qualname__
