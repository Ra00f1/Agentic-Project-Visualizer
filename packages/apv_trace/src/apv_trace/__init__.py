"""apv_trace — tiny tracing helper that ships INSIDE a target project.

Deliberately standalone: no dependency on the Agentic Project Visualizer
app, never imported by it — the app only ever tails the log file this
package writes. See `.claude/CLAUDE.md` §1 "Runtime overlay" and
`apps/backend/src/agentic_visualizer/trace/events.py` for that boundary
and the wire-format contract. This module writes plain dicts with the
same field names as `TraceEvent` by hand rather than importing it — two
independent implementations agreeing on a shape is what the JSON Schema
contract (`docs/schemas/trace-event.v1.json`) is for.

Extracted to `packages/apv_trace` (task 8) rather than vendored per
consumer (`runtime-demo-project`, `mock-agent-project/simulator`) so a
feature added for one doesn't drift out of sync with the other.

**Built on the OpenTelemetry SDK (task 13), not a hand-rolled tracer.**
`configure()` attaches a `_JsonlSpanProcessor` (`_processor.py`) to
whatever `TracerProvider` is already globally set — a target project's
own, if it got there first — or creates one if none exists. Still writes
our own `TraceEvent` JSONL, never OTLP — see `_processor.py`'s module
docstring for why.

**Two ways a span gets classified.** An `@trace`-d call carries explicit
`apv.kind`/`apv.name`/`apv.module`/`apv.qualname` attributes (set by
`_ref_for` below). An OpenInference instrumentor's span (LangChain,
LlamaIndex, Anthropic, OpenAI, ...) carries none of those — it's
classified by its own `openinference.span.kind` attribute instead (task
17; see `_openinference.py`). A target project using OpenInference needs
only `configure()` plus the instrumentor's `.instrument()` call.

Public API: call `configure()` once at process start, then either
decorate functions with `@trace(kind=...)` or instrument the project with
an OpenInference-based library. `default_log_path()` is the shared default
`configure()` resolves to when not overridden -- see its own docstring
(Task 21's D1).
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, TypeVar

import platformdirs
import ulid
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider

from ._openinference import resolve_kind_map
from ._processor import _JsonlSpanProcessor

# Real Literals, not `str` — these feed straight into the JSON line that
# `agentic_visualizer.trace.events.TraceEvent` (a closed `Literal` on both
# fields) must parse, so a typo here should be a type error at the call
# site, not a silent bad log line discovered only by the file tailer.
TraceKind = Literal["workflow", "agent", "tool", "function", "model"]
TracePhase = Literal["start", "end", "error"]

_WARN_INTERVAL_SECONDS = 60.0

_F = TypeVar("_F", bound=Callable[..., Any])


class _EmitterState:
    """Process-wide emitter state. Set once by `configure()`, read by every `@trace`.

    `tracer` is fetched eagerly here (not lazily inside `configure()`) so a
    `@trace`-d call before `configure()` has ever run still gets a real
    `Tracer` object — OTel's own `ProxyTracer` mechanism makes this
    naturally inert (a non-recording span, nothing written) until a real
    provider exists, and transparently starts working the moment
    `configure()` sets one — no manual "is configured?" branch needed
    anywhere in `trace()`.
    """

    def __init__(self) -> None:
        self.log_path: Path | None = None
        self.run_id: str | None = None
        self.manifest: dict[str, dict[str, Any]] = {}
        self.processor: _JsonlSpanProcessor | None = None
        self.tracer: trace_api.Tracer = trace_api.get_tracer("apv_trace")
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
#
# Separately: `configure()` also touches OpenTelemetry's OWN process-global
# `TracerProvider` singleton (`opentelemetry.trace.set_tracer_provider`),
# which is a second, library-owned piece of global state this module
# cooperates with rather than owns — see `configure()`'s docstring.
_state = _EmitterState()


def default_log_path() -> Path:
    """The one shared, stable trace-log path every in-repo consumer defaults
    to when no `log_path`/`APV_TRACE_LOG_PATH` override is given --
    `configure()`, the `mock-agent-project` simulator, and `apv-run` all
    resolve to this SAME path (Task 21's D1). Public so the demo, the
    simulator, and the docs can all print the exact path they're using
    instead of three different hand-maintained descriptions of it.

    One append-only file per machine, not one per run: that's what
    `FileTailTraceSource` is built for (start at the end, handle rotation),
    and three different per-consumer defaults is what let "point the
    backend at the file your target writes" have three different answers
    -- one of which changed every run -- which is what let dead letters
    silently never appear when they didn't line up. Concurrent target
    processes interleave lines in this one file; that's already how
    `apv-run` behaved, and each event's own `run_id` still tells them
    apart. Anyone who wants a separate file sets `APV_TRACE_LOG_PATH`, or
    passes `log_path` explicitly to `configure()`.
    """
    return Path(platformdirs.user_log_dir("apv")) / "trace" / "live.jsonl"


def _default_log_path() -> Path:
    override = os.environ.get("APV_TRACE_LOG_PATH")
    if override:
        return Path(override)
    return default_log_path()


def _load_manifest(manifest_path: Path) -> dict[str, dict[str, Any]]:
    """Read a `{"<kind>:<name>": {"db_id": "...", "framework_id": "..."}}` manifest.

    Missing file or malformed JSON is a warning, not a fatal error — same
    "instrumentation must never take the target project down" rule as
    `_JsonlSpanProcessor`. A target project without a manifest (or with a
    stale one after a reseed) still runs; its events just fall back to the
    resolver's lower-confidence strategies (typed-name, tool_fn) instead of
    `db_id`.
    """
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _state.warn_rate_limited(f"manifest not found at {manifest_path}, db_id will be unset")
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        _state.warn_rate_limited(f"failed to read manifest {manifest_path}: {exc}")
        return {}
    if not isinstance(raw, dict):
        _state.warn_rate_limited(f"manifest {manifest_path} is not a JSON object, ignoring")
        return {}
    # Validate each ENTRY too, not just the outer object: `_ref_for` calls
    # `entry.get(...)` on whatever's here, and that call happens outside
    # `_JsonlSpanProcessor`'s try/except (it builds the attributes dict
    # passed INTO `start_as_current_span`, before any span exists), so a
    # malformed entry (right outer shape, wrong inner shape — e.g. a
    # hand-edited manifest) would otherwise raise uncaught at call time.
    # Drop bad entries individually rather than the whole file.
    manifest: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if isinstance(entry, dict):
            manifest[key] = entry
        else:
            _state.warn_rate_limited(
                f"manifest entry {key!r} in {manifest_path} is not an object, ignoring"
            )
    return manifest


def configure(
    log_path: Path | None = None,
    run_id: str | None = None,
    manifest_path: Path | None = None,
    openinference_kinds: Mapping[str, TraceKind | None] | None = None,
) -> Path:
    """Bootstrap the emitter. Call once, before any `@trace`-d function runs
    (or before an OpenInference instrumentor's `.instrument()` call).

    `log_path` defaults to the shared `default_log_path()` (one stable file
    per machine -- Task 21's D1), overridable via `APV_TRACE_LOG_PATH`.
    Pass an explicit `log_path` (e.g. one built from `run_id`) for a
    separate file per run instead. Returns the resolved path so the caller
    can print/inspect it.

    `manifest_path`, if given, is loaded immediately so a missing/broken
    manifest warns once at startup rather than on the first traced call.

    `openinference_kinds` overrides the default OpenInference
    kind→apv-kind map (task 17); see `_openinference.resolve_kind_map` for
    the full precedence (kwarg beats the `APV_TRACE_OPENINFERENCE_KINDS`
    env var, which beats the built-in default).

    **OTel provider handling.** `get_tracer_provider()` returns whatever
    is already globally set — a no-op provider if nothing has, or a real
    SDK `TracerProvider` if the target project (or an earlier `configure()`
    call) already set one. `set_tracer_provider` only succeeds the FIRST
    time it's called in a process, so if one already exists we attach our
    processor to it instead of replacing it — correct either way: the
    target project's own exporters keep running alongside ours.

    Calling this twice shuts down the previous `_JsonlSpanProcessor` first
    (closes its own file handle) so nothing is written twice and no file
    handle leaks.
    """
    resolved_run_id = run_id or ulid.new().str
    resolved_path = log_path or _default_log_path()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    _state.log_path = resolved_path
    _state.run_id = resolved_run_id
    _state.manifest = _load_manifest(manifest_path) if manifest_path is not None else {}

    if _state.processor is not None:
        with contextlib.suppress(Exception):
            _state.processor.shutdown()

    resolved_kind_map = resolve_kind_map(
        kwarg_overrides=openinference_kinds,
        env_value=os.environ.get("APV_TRACE_OPENINFERENCE_KINDS"),
        warn=_state.warn_rate_limited,
    )

    # Deliberately not a `with` block: the handle outlives this function,
    # held open for the process lifetime like a log handler — owned by the
    # processor below for ITS lifetime (see `_JsonlSpanProcessor.shutdown`).
    file = open(resolved_path, "ab", buffering=0)  # noqa: SIM115
    processor = _JsonlSpanProcessor(
        file,
        run_id=resolved_run_id,
        warn=_state.warn_rate_limited,
        openinference_kind_map=resolved_kind_map,
    )
    _state.processor = processor

    provider = trace_api.get_tracer_provider()
    if not isinstance(provider, SDKTracerProvider):
        provider = SDKTracerProvider()
        trace_api.set_tracer_provider(provider)
    provider.add_span_processor(processor)

    _state.tracer = trace_api.get_tracer("apv_trace")
    return resolved_path


def _ref_for(fn: Callable[..., Any], kind: TraceKind, name: str) -> dict[str, Any]:
    """Build one call's `apv.*` span attributes, consulting the manifest for
    `db_id`/`framework_id`.

    Manifest lookup key is `f"{kind}:{name}"` — see `configure`'s
    `manifest_path` doc and `mock-agent-project/simulator/build_manifest.py`,
    which is the one place that key format is produced from real Mongo
    records. No manifest, or no entry for this kind+name: `apv.db_id`/
    `apv.framework_id` are both OMITTED from the returned dict (not set to
    `None` — see `_ref_from_attributes`'s docstring for why), and the
    resolver's typed-name/tool_fn strategies still work off `apv.name`/
    `apv.module`/`apv.qualname`.
    """
    entry = _state.manifest.get(f"{kind}:{name}")
    attrs: dict[str, Any] = {
        "apv.kind": kind,
        "apv.name": name,
        "apv.module": fn.__module__,
        "apv.qualname": fn.__qualname__,
    }
    if entry:
        db_id = entry.get("db_id")
        if db_id is not None:
            attrs["apv.db_id"] = db_id
        framework_id = entry.get("framework_id")
        if framework_id is not None:
            attrs["apv.framework_id"] = framework_id
    return attrs


def trace(kind: TraceKind, name: str | None = None) -> Callable[[_F], _F]:
    """Decorate a sync or async function to emit start/end/error trace events.

    `name` overrides the emitted `ref.name` for EVERY call to the decorated
    function — pass this when one function always represents the same
    catalog/DB entity (most `@trace`-d functions).

    If `name` is omitted AND the decorated function has a parameter
    literally called `name`, `ref.name` is instead read from that
    parameter's value at CALL TIME. This is for the opposite shape: one
    generic function standing in for many different entities across calls
    (`mock-agent-project/simulator`'s `simulate_agent(name, tools, ...)`
    represents whichever agent its caller names — one function definition,
    many trace identities). Falls back to `fn.__name__` if neither applies,
    same as before this parameter existed.

    The OTel **span name** (distinct from `apv.name`/`ref.name` above, and
    not currently read by anything downstream of this package — it's there
    for any OTHER exporter/tool a target project attaches to the same
    provider) is always `fn.__qualname__`, one per decorated function,
    regardless of the per-call `apv.name` resolution above.
    """

    def decorator(fn: _F) -> _F:
        try:
            sig: inspect.Signature | None = inspect.signature(fn)
        except (TypeError, ValueError):
            # Some callables (C extensions, certain functools wrappers)
            # don't support signature introspection. Fall back to the
            # fixed-name-or-__name__ behavior rather than erroring at
            # decoration time over a diagnostic nicety.
            sig = None
        has_name_param = sig is not None and "name" in sig.parameters
        span_name = fn.__qualname__

        def _resolve_attributes(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
            if name is not None:
                resolved_name = name
            elif has_name_param and sig is not None:
                bound = sig.bind_partial(*args, **kwargs)
                resolved_name = bound.arguments.get("name", fn.__name__)
                if not isinstance(resolved_name, str) or not resolved_name:
                    resolved_name = fn.__name__
            else:
                resolved_name = fn.__name__
            return _ref_for(fn, kind, resolved_name)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                # No try/except here by design: start_as_current_span's
                # defaults (record_exception=True, set_status_on_exception=
                # True) already record the exception event and ERROR status
                # AND re-raise — see _JsonlSpanProcessor.on_end /
                # _error_meta_from_span, which read exactly that back out.
                with _state.tracer.start_as_current_span(
                    span_name, attributes=_resolve_attributes(args, kwargs)
                ):
                    return await fn(*args, **kwargs)

            # mypy can't see that async_wrapper's signature matches fn's original
            # signature through functools.wraps — the two branches return the
            # same _F shape at runtime, just built dynamically on inspect.iscoroutinefunction.
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with _state.tracer.start_as_current_span(
                span_name, attributes=_resolve_attributes(args, kwargs)
            ):
                return fn(*args, **kwargs)

        # Same rationale as async_wrapper's ignore above.
        return sync_wrapper  # type: ignore[return-value]

    return decorator
