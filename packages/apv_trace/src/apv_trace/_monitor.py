"""FunctionTracer — `sys.monitoring`-based function tracing (task 18, D2).

Python 3.12+ only, by design. `sys.setprofile` fires on every call (C calls
included), can't be disabled per code location, and needs
`threading.setprofile` for threads — too slow even for dev use, and 3.12
(which has none of those problems, via PEP 669) has been out for two years.
No fallback is provided; callers on 3.11 skip this module entirely (see
`_bootstrap.py`).

**Why `PY_UNWIND` never returns `DISABLE`.** `PY_START`/`PY_RETURN` are
*local* events — returning `sys.monitoring.DISABLE` from either silences
*that one event type, for that one code object*, permanently (verified
empirically: a second, unrelated code object's events keep firing
normally). `PY_UNWIND` is not local before Python 3.15 — returning
`DISABLE` from it was verified to silence PY_UNWIND **globally, for every
code object in the process**, not just the one being classified. Getting
this wrong would silently stop all future error reporting after the first
exception through a single non-project frame. `_on_unwind` therefore
always returns `None`, costing one callback per exception-unwind through
non-project code forever — an accepted, documented cost (exceptions are
far rarer than calls).

**Reentrancy.** Every callback does real work (string building, a write)
that itself calls Python-level functions — each of which is itself a code
object `sys.monitoring` would fire `PY_START` for, recursively, since
`set_events` applies process-wide until a code object is individually
disabled. A per-thread guard (not a single global flag — callbacks fire on
every thread) makes a callback invoked from inside another callback's own
work a no-op instead of runaway recursion.
"""

from __future__ import annotations

import atexit
import contextlib
import itertools
import os
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import CodeType
from typing import TYPE_CHECKING, Any

from ._processor import _truncate

if TYPE_CHECKING:
    from ._openinference import TraceKind  # noqa: F401 - documents the "function" kind's home

_TRACEBACK_LIMIT = 4096
_EXCLUDED_DIR_NAMES = frozenset(
    {".venv", "venv", "env", ".tox", ".nox", "site-packages", "dist-packages", "node_modules"}
)
# Names `type(exc).__name__` is compared against (not `isinstance`, so this
# module never has to import `asyncio` for one class) -- see the Design's
# own "normal control flow, not failures" list.
_CONTROL_FLOW_EXCEPTIONS = frozenset({"GeneratorExit", "StopIteration", "CancelledError", "SystemExit"})

_APV_TRACE_PACKAGE_DIR = os.path.normcase(os.path.abspath(str(Path(__file__).resolve().parent)))

# `sys.monitoring` doesn't exist in typeshed under this package's configured
# `python_version = "3.11"` mypy target -- `_bootstrap.py` guarantees
# `FunctionTracer` is never constructed below 3.12, so the `else` branch
# here is unreachable at runtime; it exists only so mypy, which type-checks
# this file as if it always runs on 3.11, has something to assign. The
# `if sys.version_info >= (3, 12):` guard is what lets mypy narrow and see
# the real attribute on the `if` side, same technique used in `start()`/
# `stop()` below -- a per-module mypy config override for `python_version`
# was tried first and rejected by mypy itself ("Per-module sections should
# only specify per-module flags").
if sys.version_info >= (3, 12):
    _DISABLE: object = sys.monitoring.DISABLE
else:
    _DISABLE = None


def _now_iso() -> str:
    """Same format `_processor._ns_to_iso` produces -- there's no OTel span
    backing a function event, so there's no span start/end time to convert;
    this is simply "now", at the moment the event is built."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def module_from_path(abs_path: str, root: str) -> str:
    """The file path relative to `root`, POSIX separators, `.py` stripped,
    `/` replaced by `.` — MUST stay identical to `node_index.
    _module_from_file_path`'s transformation of an L2/L3-scanned `file_path`
    attribute (`apps/backend/tests/trace/test_monitor_module_parity.py`
    pins the two together). Deriving from the file path, not
    `frame.f_globals["__name__"]`, is what lets a `__main__` script resolve
    (a script run directly has `__name__ == "__main__"`, which carries no
    useful module information; its path does).
    """
    rel = os.path.relpath(os.path.abspath(abs_path), root).replace(os.sep, "/")
    without_suffix = rel[:-3] if rel.endswith(".py") else rel
    return without_suffix.replace("/", ".")


def _format_error(exc: BaseException) -> dict[str, str]:
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": _truncate(tb_text, _TRACEBACK_LIMIT),
    }


@dataclass(frozen=True, slots=True)
class _ActiveCall:
    span_id: str
    parent_span_id: str | None


class FunctionTracer:
    """Traces every project function call via `sys.monitoring`, writing
    `TraceEvent`-shaped dicts through `write` (typically
    `_JsonlSpanProcessor.write_event`). One instance per process; `start()`
    claims a `sys.monitoring` tool id, `stop()` (also registered with
    `atexit`) releases it.
    """

    def __init__(
        self,
        *,
        root: Path,
        write: Callable[[dict[str, Any]], None],
        run_id: str,
        max_calls: int,
        bridge: bool,
        warn: Callable[[str], None],
    ) -> None:
        self._root = os.path.normcase(os.path.abspath(str(root)))
        self._write = write
        self._run_id = run_id
        self._max_calls = max_calls
        self._bridge = bridge
        self._warn = warn

        self._active: dict[int, _ActiveCall] = {}
        self._call_counts: dict[CodeType, int] = {}
        self._filter_cache: dict[CodeType, bool] = {}
        self._reentrant = threading.local()
        # 4 random bytes, not `random` (user code may have seeded it),
        # chosen once per tracer instance and combined with a monotonic
        # counter -- together always 16 hex chars, matching the width of
        # an OTel-derived span id even though the two are generated by
        # entirely different mechanisms.
        self._span_id_prefix = os.urandom(4).hex()
        self._span_id_counter = itertools.count()
        self._tool_id: int | None = None

    def start(self) -> bool:
        """Claim a `sys.monitoring` tool id and start tracing. Returns
        `False` (and warns once) if every id is already taken by a
        debugger, coverage tool, or another profiler -- the target still
        runs, just without function tracing.

        The `sys.version_info` check below exists for this class's own
        safety (and, as a side effect, lets mypy narrow the rest of this
        method's body) -- `_bootstrap.py` already checks this before ever
        constructing a `FunctionTracer`, so it's not expected to trigger
        in practice.
        """
        if sys.version_info < (3, 12):
            self._warn("sys.monitoring requires Python 3.12+; function tracing disabled")
            return False

        for candidate in (sys.monitoring.PROFILER_ID, 4, 3):
            try:
                sys.monitoring.use_tool_id(candidate, "apv_trace")
            except ValueError:
                continue
            self._tool_id = candidate
            break
        if self._tool_id is None:
            self._warn(
                "no sys.monitoring tool id available (debugger/coverage/profiler already "
                "active); function tracing disabled"
            )
            return False

        events = sys.monitoring.events
        sys.monitoring.register_callback(self._tool_id, events.PY_START, self._on_start)
        sys.monitoring.register_callback(self._tool_id, events.PY_RETURN, self._on_return)
        sys.monitoring.register_callback(self._tool_id, events.PY_UNWIND, self._on_unwind)
        sys.monitoring.set_events(self._tool_id, events.PY_START | events.PY_RETURN | events.PY_UNWIND)
        atexit.register(self.stop)
        return True

    def stop(self) -> None:
        """Unregister the callbacks and free the tool id. Safe to call
        more than once (the `atexit` registration plus an explicit call
        from a test or `_bootstrap` both calling this is expected)."""
        tool_id = self._tool_id
        if tool_id is None:
            return
        self._tool_id = None
        if sys.version_info < (3, 12):
            return  # unreachable in practice -- tool_id is only ever set by start(), which requires 3.12+
        events = sys.monitoring.events
        with contextlib.suppress(Exception):
            sys.monitoring.set_events(tool_id, 0)
            sys.monitoring.register_callback(tool_id, events.PY_START, None)
            sys.monitoring.register_callback(tool_id, events.PY_RETURN, None)
            sys.monitoring.register_callback(tool_id, events.PY_UNWIND, None)
            sys.monitoring.free_tool_id(tool_id)

    def _enter_guard(self) -> bool:
        if getattr(self._reentrant, "active", False):
            return False
        self._reentrant.active = True
        return True

    def _exit_guard(self) -> None:
        self._reentrant.active = False

    def _is_project_code(self, code: CodeType) -> bool:
        cached = self._filter_cache.get(code)
        if cached is None:
            cached = self._classify(code)
            self._filter_cache[code] = cached
        return cached

    def _classify(self, code: CodeType) -> bool:
        if "<" in code.co_qualname:
            return False
        filename = code.co_filename
        # Frozen/built-in/synthetic code objects (`<frozen ntpath>`,
        # `<string>`, `<stdin>`, ...) use this bracket convention instead
        # of a real path. Without this check, `os.path.abspath` resolves
        # them relative to the CWD, which can land INSIDE the traced root
        # by coincidence (e.g. running from the project directory) and
        # wrongly classify core interpreter internals as project code --
        # caught empirically, not from reading the convention first.
        if filename.startswith("<"):
            return False
        path = os.path.normcase(os.path.abspath(filename))
        if not path.startswith(self._root + os.sep):
            return False
        if path.startswith(_APV_TRACE_PACKAGE_DIR + os.sep):
            return False
        return not set(Path(path).parts) & _EXCLUDED_DIR_NAMES

    def _next_span_id(self) -> str:
        return f"{self._span_id_prefix}{next(self._span_id_counter):08x}"

    def _find_parent_span_id(self, frame: Any) -> str | None:
        current = frame.f_back
        for _ in range(64):
            if current is None:
                return None
            active_call = self._active.get(id(current))
            if active_call is not None:
                return active_call.span_id
            current = current.f_back

        from opentelemetry import trace as trace_api

        current_span = trace_api.get_current_span()
        if current_span.is_recording():
            return format(current_span.get_span_context().span_id, "016x")
        return None

    def _ref_for(self, code: CodeType) -> dict[str, Any]:
        return {
            "name": code.co_name,
            "module": module_from_path(code.co_filename, self._root),
            "qualname": code.co_qualname,
            "framework_id": None,
            "db_id": None,
        }

    def _meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = {"source": "monitoring"}
        if not self._bridge:
            meta["bridge"] = False
        return meta

    def _already_reported_by_trace_decorator(self, code: CodeType) -> bool:
        """A `@trace`-d function opens its OTel span BEFORE calling the
        function body, so at `PY_START` time the current span's own
        `apv.qualname` already equals this code object's qualname --
        tracking it too would double-report every call. Only works for
        `@trace`: a raw OTel span opened INSIDE the function body starts
        after `PY_START` has already fired, so it can't be detected here.

        Checks `apv.module` too, not just `apv.qualname`: `co_qualname`
        has no module component, so two unrelated classes in different
        modules sharing a method name (`Handler.run` in both) would
        otherwise false-match while one's span happens to be current.
        """
        from opentelemetry import trace as trace_api

        current_span = trace_api.get_current_span()
        if not current_span.is_recording():
            return False
        attrs = getattr(current_span, "attributes", None)
        if attrs is None:
            return False
        return bool(attrs.get("apv.qualname") == code.co_qualname) and bool(
            attrs.get("apv.module") == module_from_path(code.co_filename, self._root)
        )

    def _on_start(self, code: CodeType, offset: int) -> object:
        if not self._enter_guard():
            return None
        try:
            if not self._is_project_code(code):
                return _DISABLE
            if self._already_reported_by_trace_decorator(code):
                return None

            count = self._call_counts.get(code, 0) + 1
            self._call_counts[code] = count
            if count > self._max_calls:
                return _DISABLE

            frame = sys._getframe(1)
            if frame.f_code is not code:
                return None

            span_id = self._next_span_id()
            parent_span_id = self._find_parent_span_id(frame)
            self._active[id(frame)] = _ActiveCall(span_id=span_id, parent_span_id=parent_span_id)
            self._write(
                {
                    "v": 1,
                    "ts": _now_iso(),
                    "run_id": self._run_id,
                    "span_id": span_id,
                    "parent_span_id": parent_span_id,
                    "phase": "start",
                    "kind": "function",
                    "ref": self._ref_for(code),
                    "meta": self._meta(),
                }
            )

            if count >= self._max_calls:
                self._warn(f"stopped tracing {code.co_qualname} after {self._max_calls} calls")
                return _DISABLE
            return None
        except Exception as exc:  # noqa: BLE001 - a monitoring callback must never crash the host project
            self._warn(f"monitor callback failed: {exc.__class__.__name__}: {exc}")
            return None
        finally:
            self._exit_guard()

    def _on_return(self, code: CodeType, offset: int, retval: object) -> object:
        if not self._enter_guard():
            return None
        try:
            if not self._is_project_code(code):
                return _DISABLE
            frame = sys._getframe(1)
            if frame.f_code is not code:
                return None
            active_call = self._active.pop(id(frame), None)
            if active_call is None:
                return None  # never started: filtered, dedup'd, or past the cap -- a cheap miss
            self._write(
                {
                    "v": 1,
                    "ts": _now_iso(),
                    "run_id": self._run_id,
                    "span_id": active_call.span_id,
                    "parent_span_id": active_call.parent_span_id,
                    "phase": "end",
                    "kind": "function",
                    "ref": self._ref_for(code),
                    "meta": self._meta(),
                }
            )
            return None
        except Exception as exc:  # noqa: BLE001 - a monitoring callback must never crash the host project
            self._warn(f"monitor callback failed: {exc.__class__.__name__}: {exc}")
            return None
        finally:
            self._exit_guard()

    def _on_unwind(self, code: CodeType, offset: int, exc: BaseException) -> None:
        if not self._enter_guard():
            return
        try:
            # NEVER `return sys.monitoring.DISABLE` here -- see module docstring.
            if not self._is_project_code(code):
                return
            frame = sys._getframe(1)
            if frame.f_code is not code:
                return
            active_call = self._active.pop(id(frame), None)
            if active_call is None:
                return

            meta = self._meta()
            exc_type_name = type(exc).__name__
            if exc_type_name in _CONTROL_FLOW_EXCEPTIONS:
                phase = "end"
                meta["unwind"] = exc_type_name
            else:
                phase = "error"
                meta["error"] = _format_error(exc)
            self._write(
                {
                    "v": 1,
                    "ts": _now_iso(),
                    "run_id": self._run_id,
                    "span_id": active_call.span_id,
                    "parent_span_id": active_call.parent_span_id,
                    "phase": phase,
                    "kind": "function",
                    "ref": self._ref_for(code),
                    "meta": meta,
                }
            )
        except Exception as inner_exc:  # noqa: BLE001 - a monitoring callback must never crash the host project
            self._warn(f"monitor callback failed: {inner_exc.__class__.__name__}: {inner_exc}")
        finally:
            self._exit_guard()
