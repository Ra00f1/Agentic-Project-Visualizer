"""Zero-code tracing bootstrap for `apv-run` (task 18).

Run from `_autoload/sitecustomize.py`, which the `apv-run` launcher puts
first on `PYTHONPATH`. `bootstrap()` configures `apv_trace` from env vars
the launcher set, auto-instruments every installed OpenInference
instrumentor, and starts the function tracer -- all without the target
project importing or calling anything itself.
"""

from __future__ import annotations

import importlib.metadata
import os
import sys
from pathlib import Path
from typing import Any

from . import configure
from ._processor import _JsonlSpanProcessor

_bootstrapped = False


def bootstrap() -> None:
    """Idempotent: a second call in the same process (two `sitecustomize`s
    both importing this, or a test calling it twice) is a no-op."""
    global _bootstrapped
    if _bootstrapped:
        return
    _bootstrapped = True

    # Protects against the autoload directory ending up on `PYTHONPATH` by
    # accident (a target that copies its own env into a child without this
    # var, or a user who manually adds the directory for some other
    # reason) -- tracing should only ever turn on because `apv-run` itself
    # set this.
    if os.environ.get("APV_TRACE_AUTOLOAD") != "1":
        return

    log_path_raw = os.environ.get("APV_TRACE_LOG_PATH")
    manifest_raw = os.environ.get("APV_TRACE_MANIFEST")
    configure(
        log_path=Path(log_path_raw) if log_path_raw else None,
        manifest_path=Path(manifest_raw) if manifest_raw else None,
    )

    instrumented, skipped = _load_openinference_instrumentors(
        os.environ.get("APV_TRACE_DISABLED_INSTRUMENTORS", "")
    )

    functions_root = os.environ.get("APV_TRACE_FUNCTIONS_ROOT")
    functions_status = "off"
    if functions_root:
        functions_status = _start_function_tracer(Path(functions_root))

    print(_format_summary(instrumented, skipped, functions_status), file=sys.stderr)


def _load_openinference_instrumentors(disabled_raw: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Instrument every installed OpenInference entry point not in the
    disabled list. Returns `(instrumented_names, [(skipped_name, reason)])`.

    Pre-checking conflicts ourselves (rather than letting `instrument()`
    do it) is what avoids the default path's ERROR-level log for every
    framework that simply isn't installed -- that would look like a
    failure in the target's own console for the common case of "only
    using LangChain, not LlamaIndex too."
    """
    instrumented: list[str] = []
    skipped: list[tuple[str, str]] = []

    if disabled_raw.strip() == "*":
        return instrumented, skipped
    disabled = {name.strip() for name in disabled_raw.split(",") if name.strip()}

    try:
        from opentelemetry.instrumentation.dependencies import get_dependency_conflicts
    except ImportError:
        # No OpenInference (or any OTel-instrumentation-based) package is
        # installed at all -- nothing to discover.
        return instrumented, skipped

    from opentelemetry import trace as trace_api

    tracer_provider = trace_api.get_tracer_provider()

    for entry_point in importlib.metadata.entry_points(group="openinference_instrumentor"):
        if entry_point.name in disabled:
            continue
        try:
            instrumentor = entry_point.load()()
            conflict = get_dependency_conflicts(instrumentor.instrumentation_dependencies())
        except Exception as exc:
            skipped.append((entry_point.name, f"{exc.__class__.__name__}: {exc}"))
            continue
        if conflict is not None:
            reason = "not installed" if not (conflict.found or conflict.found_any) else "version conflict"
            skipped.append((entry_point.name, reason))
            continue
        try:
            instrumentor.instrument(tracer_provider=tracer_provider, skip_dep_check=True)
            instrumented.append(entry_point.name)
        except Exception as exc:
            skipped.append((entry_point.name, f"failed: {exc.__class__.__name__}: {exc}"))

    return instrumented, skipped


def _start_function_tracer(root: Path) -> str:
    if sys.version_info < (3, 12):
        print(
            "[apv-run] --functions requires Python 3.12+; function tracing disabled",
            file=sys.stderr,
        )
        return "off (needs Python 3.12+)"

    from ._monitor import FunctionTracer

    processor = _current_processor()
    if processor is None:
        print("[apv-run] configure() did not produce a processor; function tracing disabled", file=sys.stderr)
        return "off (no processor)"

    max_calls_raw = os.environ.get("APV_TRACE_FUNCTIONS_MAX_CALLS", "10000")
    try:
        max_calls = int(max_calls_raw)
    except ValueError:
        max_calls = 10000
    bridge = os.environ.get("APV_TRACE_FUNCTIONS_BRIDGE") != "0"
    run_id = processor.run_id

    def write_event(event: dict[str, Any]) -> None:
        # Re-fetch the CURRENT processor on every write, not the one
        # captured above. A target that calls `configure()` again after
        # bootstrap already did (its own log-rotation, or a project like
        # `runtime-demo-project` that also configures itself outside
        # `apv-run`) replaces `_state.processor` and shuts this one down
        # -- writing through a stale bound method would otherwise
        # silently drop every function event from that point on. Found
        # empirically, not assumed: see Task 18's Result.
        current = _current_processor()
        if current is not None:
            current.write_event(event)

    tracer = FunctionTracer(
        root=root,
        write=write_event,
        run_id=run_id,
        max_calls=max_calls,
        bridge=bridge,
        warn=lambda message: print(f"[apv-run] {message}", file=sys.stderr),
    )
    started = tracer.start()
    return f"on (root={root})" if started else "off (no tool id available)"


def _current_processor() -> _JsonlSpanProcessor | None:
    from . import _state

    return _state.processor


def _format_summary(instrumented: list[str], skipped: list[tuple[str, str]], functions_status: str) -> str:
    parts = [f"instrumented: {', '.join(instrumented)}" if instrumented else "instrumented: none"]
    if skipped:
        skipped_text = ", ".join(f"{name} ({reason})" for name, reason in skipped)
        parts.append(f"skipped: {skipped_text}")
    parts.append(f"functions: {functions_status}")
    return "[apv-run] " + " | ".join(parts)
