"""apv_trace — tiny tracing helper that ships INSIDE a target project.

This package is deliberately standalone: it has no dependency on the
Agentic Project Visualizer app (`apps/backend/src/agentic_visualizer`) and
is never imported by it. The app only ever tails the log file this package
writes to — see `.claude/CLAUDE.md` §1 "Runtime overlay" and
`apps/backend/src/agentic_visualizer/trace/events.py` for the read-only
boundary and the wire-format contract this emitter must match.

Because of that boundary, this module does NOT import
`agentic_visualizer.trace.events.TraceEvent` to build events — it writes
plain dicts with the same field names by hand, verified against that schema
in `apps/backend/tests/trace/test_events.py`. Two independent
implementations agreeing on a schema is exactly what a JSON Schema contract
(`docs/schemas/trace-event.v1.json`) is for.

Public API: call `configure()` once at process start, then decorate any
sync or async function with `@trace(kind=...)`.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, TypeVar

import orjson
import platformdirs
import ulid

# Real Literals, not `str` — these feed straight into the JSON line that
# `agentic_visualizer.trace.events.TraceEvent` (a closed `Literal` on both
# fields) must parse, so a typo here should be a type error at the call
# site, not a silent bad log line discovered only by the file tailer.
TraceKind = Literal["workflow", "agent", "tool", "function"]
TracePhase = Literal["start", "end", "error"]

_TRACEBACK_LIMIT = 4096
_WARN_INTERVAL_SECONDS = 60.0

# ContextVar, not threading.local: asyncio tasks share an OS thread, and a
# ContextVar is copied per-task (Python 3.11+ default task factory), so
# concurrent async spans nest correctly without stepping on each other's
# "current span" state. threading.local would leak one shared value across
# every concurrently-running task on the same thread.
_current_span: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "apv_current_span", default=None
)

_F = TypeVar("_F", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class _SpanIdentity:
    """The coordinates that stay constant across one span's start/end/error events.

    Bundled into one object (CLAUDE.md §7: "more than 4 parameters →
    consider a dataclass") — `_emit` would otherwise take kind/span_id/
    parent_span_id/ref as four separate arguments, at three call sites.
    """

    kind: TraceKind
    span_id: str
    parent_span_id: str | None
    ref: dict[str, Any]


class _EmitterState:
    """Process-wide emitter state. Set once by `configure()`, read by every `@trace`."""

    def __init__(self) -> None:
        self.log_path: Path | None = None
        self.run_id: str | None = None
        self._file: BinaryIO | None = None
        self._write_lock = threading.Lock()
        # Separate from _write_lock on purpose: rate-limit bookkeeping and
        # the file write protect unrelated state. Sharing one lock would
        # serialize a fast timestamp check behind a slow write.
        self._warn_lock = threading.Lock()
        self._last_warn_at = 0.0

    def warn_rate_limited(self, message: str) -> None:
        """Surface a swallowed emitter failure on stderr, at most once a minute.

        Instrumentation must never take the target project down — every
        failure path here is swallowed — but a silently-broken trace log
        (disk full, file locked by another process) should still be visible
        somewhere without spamming the target project's own logs.
        """
        now = time.monotonic()
        with self._warn_lock:
            if now - self._last_warn_at < _WARN_INTERVAL_SECONDS:
                return
            self._last_warn_at = now
        # Suppresses, not logs, because stderr itself is the thing that could
        # be broken here — there is nowhere left to report the failure to.
        with contextlib.suppress(Exception):
            print(f"[apv_trace] {message}", file=sys.stderr)


# Deliberate module-level singleton (flagged per CLAUDE.md §12). `configure()`
# and `@trace` are called from arbitrary, unrelated modules across whatever
# project imports this file, with no way to thread a shared object through
# every call site — the same reason Python's own `logging` module keeps its
# handler registry at module scope. Doesn't apply to the visualizer app
# itself: `apv_trace` never runs inside it, only inside a target project.
_state = _EmitterState()


def _default_log_path(run_id: str) -> Path:
    override = os.environ.get("APV_TRACE_LOG_PATH")
    if override:
        return Path(override)
    return Path(platformdirs.user_log_dir("apv")) / "trace" / f"{run_id}.jsonl"


def configure(log_path: Path | None = None, run_id: str | None = None) -> Path:
    """Bootstrap the emitter. Call once, before any `@trace`-d function runs.

    `log_path` defaults to `platformdirs.user_log_dir("apv") / "trace" /
    "<run_id>.jsonl"`, overridable via the `APV_TRACE_LOG_PATH` environment
    variable — this is the one place that default lives; callers (e.g.
    `runtime_demo/run_demo.py`) don't need their own copy of this logic.
    Returns the resolved path so the caller can print/inspect it.
    """
    resolved_run_id = run_id or ulid.new().str
    resolved_path = log_path or _default_log_path(resolved_run_id)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    _state.log_path = resolved_path
    _state.run_id = resolved_run_id
    # Unbuffered: a killed process shouldn't lose events sitting in a
    # Python-level buffer. Trade-off: every write is its own OS write() call
    # rather than a batched one — acceptable at fixture/demo call volumes;
    # flagged as a follow-up if a real target project needs higher throughput.
    # Deliberately not a `with` block: the handle outlives this function,
    # held open for the process lifetime like a log handler.
    _state._file = open(resolved_path, "ab", buffering=0)  # noqa: SIM115
    return resolved_path


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _emit(phase: TracePhase, identity: _SpanIdentity, meta: dict[str, Any] | None = None) -> None:
    """Write one trace event line. Must never raise.

    Every failure — serialization, the write itself, even event-dict
    construction — is caught below. `warn_rate_limited` (its own try/except)
    is the last line of defense; if even that fails there is truly nowhere
    left to report to, and we drop it silently rather than risk taking the
    target project down over a diagnostic channel.
    """
    if _state.log_path is None or _state._file is None:
        # configure() was never called. Tracing is inert, not fatal — a
        # target project shouldn't crash just because nobody turned tracing on.
        return
    try:
        event = {
            "v": 1,
            "ts": datetime.now(UTC).isoformat(timespec="microseconds"),
            "run_id": _state.run_id,
            "span_id": identity.span_id,
            "parent_span_id": identity.parent_span_id,
            "phase": phase,
            "kind": identity.kind,
            "ref": identity.ref,
            "meta": meta or {},
        }
        line = orjson.dumps(event)
        with _state._write_lock:
            _state._file.write(line + b"\n")
    except Exception as exc:  # noqa: BLE001 - instrumentation must never crash the host project
        _state.warn_rate_limited(f"failed to write trace event: {exc.__class__.__name__}: {exc}")


def _ref_for(fn: Callable[..., Any], name: str | None) -> dict[str, Any]:
    return {
        "name": name or fn.__name__,
        "module": fn.__module__,
        "qualname": fn.__qualname__,
        "framework_id": None,
        "db_id": None,
    }


def _error_meta(exc: BaseException) -> dict[str, Any]:
    return {
        "error": {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": _truncate(traceback.format_exc(), _TRACEBACK_LIMIT),
        }
    }


@contextlib.contextmanager
def _span(kind: TraceKind, ref: dict[str, Any]) -> Iterator[None]:
    """Emit start/end/error around one traced call; always restores `_current_span`.

    The `finally` wraps the *entire* body, including the "start" emit call —
    if `_emit` ever raised despite its own try/except, the ContextVar token
    would otherwise leak and corrupt `parent_span_id` chaining for every
    later `@trace` call on this task/thread for the rest of the process.
    """
    span_id = ulid.new().str
    parent = _current_span.get()
    identity = _SpanIdentity(kind=kind, span_id=span_id, parent_span_id=parent, ref=ref)
    token = _current_span.set(span_id)
    try:
        _emit("start", identity)
        try:
            yield
        except Exception as exc:
            _emit("error", identity, meta=_error_meta(exc))
            raise
        else:
            _emit("end", identity)
    finally:
        _current_span.reset(token)


def trace(kind: TraceKind, name: str | None = None) -> Callable[[_F], _F]:
    """Decorate a sync or async function to emit start/end/error trace events.

    `name` overrides the emitted `ref.name` — pass the tool's name as known
    by whatever catalog/DB the target project registers it in, so the
    visualizer's node-resolution step (task NN+2) can match on it directly
    instead of falling back to module+qualname matching.
    """

    def decorator(fn: _F) -> _F:
        ref = _ref_for(fn, name)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with _span(kind, ref):
                    return await fn(*args, **kwargs)

            # mypy can't see that async_wrapper's signature matches fn's original
            # signature through functools.wraps — the two branches return the
            # same _F shape at runtime, just built dynamically on inspect.iscoroutinefunction.
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with _span(kind, ref):
                return fn(*args, **kwargs)

        # Same rationale as async_wrapper's ignore above.
        return sync_wrapper  # type: ignore[return-value]

    return decorator
