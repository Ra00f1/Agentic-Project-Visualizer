"""The `SpanProcessor` that turns OTel spans into `TraceEvent` JSONL lines.

Split out of `__init__.py` (task 17) because that file had grown past
CLAUDE.md §5's ~300-line limit once this task's OpenInference classification
was going to add to it. `run_id` and `warn` come in through the constructor
rather than being read from `apv_trace._state` at write time -- that's what
makes this module importable without importing `__init__.py` back (a cycle,
since `__init__.py` imports `_JsonlSpanProcessor` from here). `_state` itself
stays in `__init__.py` untouched, so the test fixture that resets it keeps
working unchanged.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, BinaryIO, Literal

import orjson
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import StatusCode

from ._openinference import OI_SPAN_KIND_ATTR, classify, is_start_eligible, resolve_name

# See `_openinference.TraceKind`'s own comment: duplicated, not imported
# from `__init__.py`, to avoid a circular import.
TraceKind = Literal["workflow", "agent", "tool", "function", "model"]

_TRACEBACK_LIMIT = 4096
_MAX_LIVE_SPANS = 10_000

logger = logging.getLogger("apv_trace")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _ns_to_iso(ns: int | None) -> str:
    """OTel span timestamps are epoch nanoseconds; `TraceEvent.ts` wants an
    ISO-8601 string. `ns` is only `None` for a span OTel itself never
    started/ended, which can't happen for spans this processor acts on (it
    only ever sees spans already past `on_start`/`on_end`) — the fallback
    to "now" exists purely so a type-narrowing bug here degrades to a
    slightly-off timestamp, never a crash."""
    if ns is None:
        return datetime.now(UTC).isoformat(timespec="microseconds")
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=UTC).isoformat(timespec="microseconds")


def _ref_from_attributes(attrs: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct the `TraceRef`-shaped `ref` dict from an `@trace`-d span's
    `apv.*` attributes — the inverse of `_ref_for`. `apv.db_id`/
    `apv.framework_id` were OMITTED (not set to `None`) when absent at
    span-creation time, so `.get(...)` here is what turns "absent" back
    into the explicit `None` the wire format wants.
    """
    return {
        "name": attrs["apv.name"],
        "module": attrs["apv.module"],
        "qualname": attrs.get("apv.qualname"),
        "framework_id": attrs.get("apv.framework_id"),
        "db_id": attrs.get("apv.db_id"),
    }


def _ref_from_openinference(oi_kind: str, attrs: Mapping[str, Any], span: ReadableSpan) -> dict[str, Any]:
    """Build the `ref` dict for an OpenInference-derived event.

    `qualname`/`framework_id`/`db_id` stay `None`: with no qualname and a
    module no graph node has, `NodeIndex`'s `module_qualname`/`tool_fn`
    strategies can't produce a false match off an OpenInference span --
    `typed_name` (and `model_prefix` for `model`-kind events) does the
    resolving instead.
    """
    return {
        "name": resolve_name(oi_kind, attrs, span.name),
        "module": _scope_name(span),
        "qualname": None,
        "framework_id": None,
        "db_id": None,
    }


def _scope_name(span: ReadableSpan) -> str:
    """`TraceRef.module` must be non-empty; `"openinference"` is the fallback
    if `instrumentation_scope` is ever missing (verified present on the
    installed SDK 1.45.1, but an OpenInference span is never guaranteed to
    come from a scope with a name)."""
    scope = getattr(span, "instrumentation_scope", None)
    name = getattr(scope, "name", None) if scope is not None else None
    return name if isinstance(name, str) and name else "openinference"


def _error_meta_from_span(span: ReadableSpan) -> dict[str, Any]:
    """Build `meta["error"]` from the span's SDK-recorded exception event.

    `start_as_current_span(..., record_exception=True)` (the default
    `@trace` relies on) makes the SDK attach an `"exception"` event with
    `exception.type`/`exception.message`/`exception.stacktrace`
    attributes itself. A LangChain chain error has no exception event at
    all (the instrumentor sets status ERROR directly) — the fallback to
    the span's status description is what makes that case produce a
    sensible line instead of an empty one.
    """
    exception_event = next((e for e in span.events if e.name == "exception"), None)
    if exception_event is None:
        return {
            "type": "Error",
            "message": span.status.description or "",
            "traceback": "",
        }
    event_attrs = exception_event.attributes or {}
    return {
        "type": str(event_attrs.get("exception.type", "Error")),
        "message": str(event_attrs.get("exception.message", "")),
        "traceback": _truncate(str(event_attrs.get("exception.stacktrace", "")), _TRACEBACK_LIMIT),
    }


class _JsonlSpanProcessor(SpanProcessor):
    """Writes `start`/`end`/`error` `TraceEvent` JSONL lines from OTel spans.

    **Why our own JSONL, not an OTLP exporter.** OTLP exporters run on span
    *end* — a call still in flight would be invisible. `on_start` firing
    synchronously is what lets a `start` line hit disk while the call is
    still running.

    **Two classification paths.** A span with `apv.kind` (from `@trace`) is
    written live at `on_start`, as before this task. A span with
    `openinference.span.kind` instead goes through `_openinference.
    classify`/`resolve_name`, and is often not known yet at `on_start` —
    see `on_start`/`on_end`'s docstrings for why that forces deferred
    emission for most OpenInference spans. `apv.kind` wins if both are
    present. A span with neither is skipped (silently to the JSONL; a
    DEBUG log fires from `on_end`, not `on_start` — see its docstring).

    Owns ONE file handle for its own lifetime, captured at construction —
    NOT read from `apv_trace._state` at write time, so a `configure()`
    called twice can't have the OLD processor's `shutdown()` close the
    NEW file `_state` has already moved on to. `run_id` and `warn` are
    captured the same way, for the unrelated reason that reading `_state`
    here at all would import `__init__.py` back into this module (a
    cycle) — see this module's own docstring.
    """

    def __init__(
        self,
        file: BinaryIO,
        *,
        run_id: str,
        warn: Callable[[str], None],
        openinference_kind_map: Mapping[str, TraceKind | None],
    ) -> None:
        self._file = file
        self._write_lock = threading.Lock()
        self._closed = False
        self._run_id = run_id
        self._warn = warn
        self._openinference_kind_map = dict(openinference_kind_map)
        # Insertion-ordered so the cap eviction below can always evict the
        # OLDEST entry in O(1) -- a span that never ends (killed target
        # process, or an instrumentor bug that never calls span.end())
        # must not grow this dict without bound.
        self._live: OrderedDict[int, tuple[TraceKind, dict[str, Any], dict[str, Any]]] = OrderedDict()
        self._live_lock = threading.Lock()

    @property
    def run_id(self) -> str:
        """Exposed for producers that aren't spans (task 18's `FunctionTracer`,
        which builds its own event dicts directly rather than going through
        OTel) -- they need the same `run_id` every event in this run carries."""
        return self._run_id

    def write_event(self, event: dict[str, Any]) -> None:
        """Write an already-built `TraceEvent`-shaped dict, for a producer
        that isn't an OTel span at all. Same guarantees as the span-derived
        write path: honors `_closed`, takes the write lock, and never
        raises -- a write failure is reported through `warn` instead."""
        if self._closed:
            return
        try:
            self._write(event)
        except Exception as exc:  # noqa: BLE001 - instrumentation must never crash the host project
            self._warn(f"failed to write trace event: {exc.__class__.__name__}: {exc}")

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        """Write a live `start` line now if we already know enough to.

        An `@trace`-d span always qualifies. An OpenInference span
        qualifies only if its kind is mapped AND its kind's primary name
        attribute (`_openinference._primary_name_attr`) is already set —
        true for an instrumentor that sets everything at span creation,
        false for LangChain's, which calls `start_span` with no attributes
        and fills them in just before the span ends. That span (and every
        LLM span whose model name is only known after the call) is
        deferred to `on_end` instead. Nothing is logged here for an
        unclassifiable span — `on_end` does that once, when the span's
        fate is actually known.
        """
        if self._closed:
            return
        attrs = span.attributes
        if attrs is not None and "apv.kind" in attrs:
            self._write_start(span, str(attrs["apv.kind"]), _ref_from_attributes(attrs))
            return

        oi_kind_raw = attrs.get(OI_SPAN_KIND_ATTR) if attrs is not None else None
        if not isinstance(oi_kind_raw, str):
            return
        apv_kind = classify(oi_kind_raw, self._openinference_kind_map)
        if apv_kind is None or not is_start_eligible(oi_kind_raw, attrs):
            return

        assert attrs is not None  # oi_kind_raw came from attrs.get(...) above, so attrs can't be None here
        ref = _ref_from_openinference(oi_kind_raw, attrs, span)
        meta = {"source": "openinference", "oi_span_kind": oi_kind_raw}
        self._write_start(span, apv_kind, ref, meta)
        self._remember_live(span, apv_kind, ref, meta)

    def on_end(self, span: ReadableSpan) -> None:
        """Write the matching `end`/`error` line, or the deferred pair.

        Three cases: (1) an `@trace`-d span — same as before. (2) a span
        already written live in `on_start` — write `end`/`error` using the
        **stored** ref from `_live`, never re-derived: if `agent.name`
        changed mid-span, the end event must still resolve to the same
        node the start did, or that node stays `active` until the orphan
        TTL sweep. (3) a deferred OpenInference span, classified for the
        first time now that its final attributes are visible — write
        `start` (backdated to `span.start_time`) then `end`/`error`.

        The "skipping unrelated span" debug log lives here, not in
        `on_start`, because of case 3: a LangChain span has no attributes
        at `on_start`, so logging the skip there would fire for spans
        that get classified a moment later.
        """
        if self._closed:
            return

        context = span.get_span_context()
        span_id = context.span_id if context else None
        live_entry = self._pop_live(span_id) if span_id is not None else None
        if live_entry is not None:
            apv_kind, ref, meta = live_entry
            self._write_end_or_error(span, apv_kind, ref, meta)
            return

        attrs = span.attributes
        if attrs is not None and "apv.kind" in attrs:
            self._write_end_or_error(span, str(attrs["apv.kind"]), _ref_from_attributes(attrs))
            return

        oi_kind_raw = attrs.get(OI_SPAN_KIND_ATTR) if attrs is not None else None
        if not isinstance(oi_kind_raw, str):
            logger.debug("skipping span without apv.kind or openinference.span.kind: name=%r", span.name)
            return
        deferred_kind = classify(oi_kind_raw, self._openinference_kind_map)
        if deferred_kind is None:
            logger.debug(
                "skipping unmapped openinference span: name=%r oi_kind=%r", span.name, oi_kind_raw
            )
            return

        assert attrs is not None  # oi_kind_raw came from attrs.get(...) above, so attrs can't be None here
        ref = _ref_from_openinference(oi_kind_raw, attrs, span)
        meta = {"source": "openinference", "oi_span_kind": oi_kind_raw}
        self._write_start(span, deferred_kind, ref, meta, ts_override=span.start_time)
        self._write_end_or_error(span, deferred_kind, ref, meta)

    def _envelope(self, span: ReadableSpan, phase: str, apv_kind: str, ref: dict[str, Any]) -> dict[str, Any]:
        context = span.get_span_context()
        return {
            "v": 1,
            "run_id": self._run_id,
            "span_id": format(context.span_id, "016x") if context else None,
            "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
            "phase": phase,
            "kind": apv_kind,
            "ref": ref,
        }

    def _write_start(
        self,
        span: ReadableSpan,
        apv_kind: str,
        ref: dict[str, Any],
        meta: dict[str, Any] | None = None,
        *,
        ts_override: int | None = None,
    ) -> None:
        try:
            event = self._envelope(span, "start", apv_kind, ref)
            event["ts"] = _ns_to_iso(ts_override if ts_override is not None else span.start_time)
            event["meta"] = dict(meta) if meta else {}
            self._write(event)
        except Exception as exc:  # noqa: BLE001 - instrumentation must never crash the host project
            self._warn(f"failed to write trace event: {exc.__class__.__name__}: {exc}")

    def _write_end_or_error(
        self,
        span: ReadableSpan,
        apv_kind: str,
        ref: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> None:
        try:
            is_error = span.status.status_code == StatusCode.ERROR
            event = self._envelope(span, "error" if is_error else "end", apv_kind, ref)
            event["ts"] = _ns_to_iso(span.end_time)
            event_meta = dict(meta) if meta else {}
            if is_error:
                event_meta["error"] = _error_meta_from_span(span)
            event["meta"] = event_meta
            self._write(event)
        except Exception as exc:  # noqa: BLE001 - instrumentation must never crash the host project
            self._warn(f"failed to write trace event: {exc.__class__.__name__}: {exc}")

    def _remember_live(
        self, span: Span, apv_kind: TraceKind, ref: dict[str, Any], meta: dict[str, Any]
    ) -> None:
        context = span.get_span_context()
        if context is None:
            return
        with self._live_lock:
            if len(self._live) >= _MAX_LIVE_SPANS and context.span_id not in self._live:
                self._live.popitem(last=False)
                self._warn(
                    f"openinference live-span cache full ({_MAX_LIVE_SPANS}); evicting the oldest entry"
                )
            self._live[context.span_id] = (apv_kind, ref, meta)

    def _pop_live(self, span_id: int) -> tuple[TraceKind, dict[str, Any], dict[str, Any]] | None:
        with self._live_lock:
            return self._live.pop(span_id, None)

    def _write(self, event: dict[str, Any]) -> None:
        line = orjson.dumps(event)
        with self._write_lock:
            self._file.write(line + b"\n")

    def shutdown(self) -> None:
        """Make both hooks permanent no-ops and close this processor's own
        file handle. Safe to call from `configure()` on a processor that's
        still registered on a shared `TracerProvider` — OTel keeps calling
        `on_start`/`on_end` on it for the provider's lifetime, so `_closed`
        is what makes those calls harmless rather than ever unregistering
        the processor (the SDK has no public "remove processor" API)."""
        self._closed = True
        with contextlib.suppress(Exception):
            self._file.close()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        # Every write is already an unbuffered, synchronous OS write() call
        # (see `configure`'s `open(..., buffering=0)`) — nothing is ever
        # queued, so there is nothing to flush.
        return True
