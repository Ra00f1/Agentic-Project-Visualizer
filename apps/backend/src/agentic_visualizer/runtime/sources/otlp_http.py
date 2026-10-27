"""OtlpHttpTraceSource -- a loopback-only OTLP/HTTP receiver (Task 23).

Lets a target project that already speaks OpenTelemetry light up the graph
with NO `apv_trace` dependency: it points its own standard OTLP exporter at
this app (`OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:<port>`) and this
source accepts the `POST /v1/traces` export, classifies each span via
`span_mapping.classify`, and turns it into the same `start`/`end`/`error`
`TraceEvent` pairs the file-tail method produces.

**A second uvicorn server in this process (CLAUDE.md §12 flag).** `start()`
runs a second `uvicorn.Server` as a background asyncio task, alongside the
app's own HTTP server. It is loopback-only (`127.0.0.1`), exists only while
this trace method is enabled, and `stop()`/`RuntimeChannel.stop()` tears it
down the same way any other trace source is torn down.

**Binding the socket ourselves, not through `uvicorn.Config(host=, port=)`.**
Uvicorn's own `Server.startup()` calls `sys.exit()` on a port-bind `OSError`
-- correct for its normal "I am the process" use case, catastrophic here
(a port clash on an *optional* overlay method must not kill the whole
backend). Binding a plain `socket` ourselves first, catching `OSError`
there, and handing the already-open socket to `server.serve(sockets=...)`
routes around that code path entirely: uvicorn never attempts its own
bind, so it never reaches the `sys.exit` branch.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import io
import json
import logging
import platform
import socket
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import uvicorn
from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agentic_visualizer.runtime.messages import SourceStatus
from agentic_visualizer.runtime.span_mapping import classify
from agentic_visualizer.trace.events import TraceEvent, TraceRef

logger = logging.getLogger("agentic_visualizer")

_MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_DECOMPRESSED_BYTES = _MAX_BODY_BYTES * 8
"""A gzip body passes the compressed-size cap (`_MAX_BODY_BYTES`) while
still expanding far past it -- a handful of MB of repeated bytes gzips to
almost nothing. `_gzip_decompress_bounded` enforces this cap on the
DECOMPRESSED size, reading in chunks rather than calling `gzip.decompress`
(which has no size limit of its own and would materialize the full,
possibly-huge buffer before we ever get a chance to reject it)."""
_TRACEBACK_LIMIT = 4096
_HEX_ID_KEYS = ("traceId", "spanId", "parentSpanId")
"""OTLP JSON's camelCase field names for the three id fields that are
hex-encoded, not base64 -- see `_otlp_json_to_proto`'s docstring."""

_ParseFailure = (
    DecodeError,
    json_format.ParseError,
    ValidationError,
    ValueError,
    TypeError,
    EOFError,  # a truncated/corrupt gzip stream raises this, not BadGzipFile
    gzip.BadGzipFile,
)
"""Every exception a malformed export body (or one malformed span within
an otherwise-valid one -- see `_handle_export`'s per-span try/except,
which also uses this tuple) can legitimately raise -- caught so a bad
request/span always becomes a 400 or a skipped span + `parse_error` count,
never an unhandled exception into uvicorn (`json.JSONDecodeError` is a
`ValueError` subclass; bad hex from `bytes.fromhex` is also `ValueError`;
`ValidationError` -- raised by `TraceRef`/`TraceEvent` construction on a
malformed-but-otherwise-plausible span, e.g. an empty name -- is also a
`ValueError` subclass, listed explicitly for clarity since it's caught at
a different call site than the others)."""


def _attrs_to_dict(attributes: Iterable[KeyValue]) -> dict[str, Any]:
    """Flatten a repeated `KeyValue` (resource/scope/span attributes) into a
    plain dict. Only scalar value kinds are extracted -- `array_value`/
    `kvlist_value`/`bytes_value` are skipped (no semantic-convention
    attribute `span_mapping.classify` reads is ever one of those)."""
    result: dict[str, Any] = {}
    for kv in attributes:
        value = kv.value
        kind = value.WhichOneof("value")
        if kind == "string_value":
            result[kv.key] = value.string_value
        elif kind == "bool_value":
            result[kv.key] = value.bool_value
        elif kind == "int_value":
            result[kv.key] = value.int_value
        elif kind == "double_value":
            result[kv.key] = value.double_value
    return result


def _ns_to_datetime(nanos: int) -> datetime:
    return datetime.fromtimestamp(nanos / 1_000_000_000, tz=UTC)


def _gzip_decompress_bounded(data: bytes, max_bytes: int) -> bytes:
    """Decompress `data`, raising `ValueError` if the DECOMPRESSED size
    would exceed `max_bytes` -- a zip-bomb defense `gzip.decompress` alone
    doesn't provide (see `_MAX_DECOMPRESSED_BYTES`'s own comment). Reads in
    chunks and aborts as soon as the cap is crossed, rather than fully
    decompressing first and checking after, so a malicious body can't force
    the full oversized buffer into memory before being rejected."""
    chunks: list[bytes] = []
    total = 0
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
        while True:
            chunk = gz.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"decompressed OTLP body exceeds {max_bytes} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


def _error_meta_from_span(span: Span) -> dict[str, str]:
    """Mirrors `apv_trace._processor._error_meta_from_span`: prefer the
    span's recorded `exception` event, fall back to the status message."""
    exception_event = next((e for e in span.events if e.name == "exception"), None)
    if exception_event is None:
        return {"type": "Error", "message": span.status.message, "traceback": ""}
    event_attrs = _attrs_to_dict(exception_event.attributes)
    traceback = str(event_attrs.get("exception.stacktrace", ""))
    if len(traceback) > _TRACEBACK_LIMIT:
        traceback = traceback[:_TRACEBACK_LIMIT] + "...<truncated>"
    return {
        "type": str(event_attrs.get("exception.type", "Error")),
        "message": str(event_attrs.get("exception.message", "")),
        "traceback": traceback,
    }


def _span_to_events(
    resource_attrs: dict[str, Any], scope_name: str | None, span: Span
) -> tuple[TraceEvent, TraceEvent] | None:
    """Rule 2 of the task's "Converting each span": merge attributes (span
    wins), classify, and build the `start`/`end-or-error` pair. `None` means
    `span_mapping.classify` didn't recognize this span -- counted as
    `events_ignored`, not a dead letter (an HTTP/DB span from auto-
    instrumentation would otherwise flood the dead-letter buffer)."""
    attrs = {**resource_attrs, **_attrs_to_dict(span.attributes)}
    classified = classify(attrs, span.name, scope_name)
    if classified is None:
        return None

    run_id = str(resource_attrs.get("service.name") or "otlp")
    span_id = span.span_id.hex()
    parent_span_id = span.parent_span_id.hex() or None
    ref = TraceRef(**classified.ref)
    meta: dict[str, Any] = {"source": "otlp", "delivery": "completed", **classified.meta}

    start_event = TraceEvent(
        ts=_ns_to_datetime(span.start_time_unix_nano),
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        phase="start",
        kind=classified.kind,
        ref=ref,
        meta=meta,
    )
    is_error = span.status.code == Status.STATUS_CODE_ERROR
    end_meta = dict(meta)
    if is_error:
        end_meta["error"] = _error_meta_from_span(span)
    end_event = TraceEvent(
        ts=_ns_to_datetime(span.end_time_unix_nano),
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        phase="error" if is_error else "end",
        kind=classified.kind,
        ref=ref,
        meta=end_meta,
    )
    return start_event, end_event


def _walk_spans(request: ExportTraceServiceRequest) -> list[tuple[dict[str, Any], str | None, Span]]:
    """Walk `resource_spans -> scope_spans -> spans`, pairing each span with
    its already-merged resource attributes and its scope's name."""
    walked: list[tuple[dict[str, Any], str | None, Span]] = []
    for resource_spans in request.resource_spans:
        resource_attrs = _attrs_to_dict(resource_spans.resource.attributes)
        for scope_spans in resource_spans.scope_spans:
            scope_name = scope_spans.scope.name or None
            for span in scope_spans.spans:
                walked.append((resource_attrs, scope_name, span))
    return walked


def _otlp_json_to_proto(body: bytes) -> ExportTraceServiceRequest:
    """OTLP JSON differs from the standard protobuf JSON mapping: `traceId`/
    `spanId`/`parentSpanId` are hex strings, not base64 (verified against
    the OTLP spec's JSON encoding section, 2026-10-22). Converting those
    three keys from hex to base64 before `json_format.ParseDict` lets the
    rest of the structure (attributes, timestamps, status, ...) parse as
    plain protobuf JSON, unmodified.
    """
    parsed = json.loads(body)
    for resource_spans in parsed.get("resourceSpans", []):
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                for key in _HEX_ID_KEYS:
                    value = span.get(key)
                    if isinstance(value, str) and value:
                        span[key] = base64.b64encode(bytes.fromhex(value)).decode("ascii")
    request = ExportTraceServiceRequest()
    json_format.ParseDict(parsed, request, ignore_unknown_fields=True)
    return request


@dataclass(frozen=True, slots=True)
class OtlpHttpStats:
    """Health counters for a running `OtlpHttpTraceSource` -- same role,
    and same one-call-via-`.stats()` shape, as `FileTailStats`, so
    `RuntimeChannel._aggregate_source_stats` can read both source types
    through one consistent pattern instead of branching into two
    differently-shaped access styles for the same two counters."""

    dropped_count: int
    """Events evicted from the bounded queue because a consumer fell behind."""

    parse_error_count: int
    """Export bodies rejected as malformed (400), PLUS individual spans
    that classified but failed to build into a valid `TraceEvent` -- see
    `_handle_export`'s per-span try/except."""


class OtlpHttpTraceSource:
    """Runs a second `uvicorn.Server` on `127.0.0.1:<port>`, serving
    `POST /v1/traces`."""

    def __init__(self, port: int, *, max_queue: int = 1000) -> None:
        self._port = port
        self._queue: asyncio.Queue[TraceEvent] = asyncio.Queue(maxsize=max_queue)
        self._server: uvicorn.Server | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._dropped_count = 0
        self._parse_error_count = 0
        self._ignored_count = 0
        self._error: str | None = None

    def status(self) -> SourceStatus:
        if self._error is not None:
            return SourceStatus(
                method_id="", label="", state="error", detail=self._error, events_received=0, events_ignored=0
            )
        return SourceStatus(
            method_id="",
            label="",
            state="running",
            detail=f"listening on 127.0.0.1:{self._port}",
            events_received=0,
            events_ignored=0,
        )

    def stats(self) -> OtlpHttpStats:
        """Snapshot of the health counters. Safe to call from any task."""
        return OtlpHttpStats(dropped_count=self._dropped_count, parse_error_count=self._parse_error_count)

    async def start(self) -> None:
        if self._serve_task is not None:
            raise RuntimeError("OtlpHttpTraceSource is already started")
        self._stop_event.clear()
        self._error = None
        try:
            sock = await asyncio.to_thread(self._bind_socket)
        except OSError:
            self._error = f"port {self._port} is in use — change it in Setup"
            return
        app = self._build_app()
        config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="off")
        server = uvicorn.Server(config)
        self._server = server
        self._serve_task = asyncio.create_task(
            server.serve(sockets=[sock]), name=f"runtime_otlp_http_{self._port}"
        )
        self._serve_task.add_done_callback(self._on_serve_task_done)

    def _on_serve_task_done(self, task: asyncio.Task[None]) -> None:
        """A `server.serve()` that ends on its own (not via `stop()`'s
        `task.cancel()`) means the listener silently died -- without this,
        `status()` would keep reporting "running" forever (it only ever
        checked `self._error`, which nothing else ever set after a
        successful start). `stop()` cancels the task before awaiting it,
        so `task.cancelled()` is exactly how this callback tells "we did
        this on purpose" apart from "it crashed."
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("runtime: otlp_http listener on port %d died: %s", self._port, exc)
            self._error = f"listener on port {self._port} stopped unexpectedly: {exc}"

    def _bind_socket(self) -> socket.socket:
        """Binds synchronously (run via `asyncio.to_thread`) so a blocking
        DNS/firewall-prompt stall on `bind()` can't freeze the event loop.

        **Windows-specific: `SO_EXCLUSIVEADDRUSE`, not `SO_REUSEADDR`.**
        `SO_REUSEADDR` means something different on Windows than on POSIX --
        it lets a second socket silently rebind a port another socket is
        ALREADY listening on, with no `OSError` (unlike POSIX, where it
        only permits rebinding a port stuck in `TIME_WAIT`). That would
        make the port-already-in-use detection this method exists for
        simply not fire on Windows. `SO_EXCLUSIVEADDRUSE` is the
        Windows-specific option that actually enforces exclusivity (verified
        empirically: a port held by a real listening socket raises `OSError`
        here with this flag, and silently succeeds without it).
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if platform.system() == "Windows":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self._port))
        sock.listen(2048)
        sock.setblocking(False)
        return sock

    async def stop(self) -> None:
        """Idempotent, same contract as `FileTailTraceSource.stop()`."""
        self._stop_event.set()
        if self._server is not None:
            self._server.should_exit = True
        task, self._serve_task = self._serve_task, None
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a dying serve() must not wedge stop()
                logger.exception("runtime: otlp_http serve() on port %d ended with an error", self._port)
        self._server = None
        while not self._queue.empty():
            self._queue.get_nowait()

    async def events(self) -> AsyncIterator[TraceEvent]:
        """Same stop-race pattern as `FileTailTraceSource.events()` -- see
        that method's docstring for why both waiters are needed."""
        stop_waiter = asyncio.ensure_future(self._stop_event.wait())
        get_waiter: asyncio.Task[TraceEvent] | None = None
        try:
            while True:
                get_waiter = asyncio.ensure_future(self._queue.get())
                done, _pending = await asyncio.wait(
                    {get_waiter, stop_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                if get_waiter in done:
                    result = get_waiter.result()
                    get_waiter = None
                    yield result
                    continue
                get_waiter.cancel()
                get_waiter = None
                return
        finally:
            stop_waiter.cancel()
            if get_waiter is not None:
                get_waiter.cancel()

    def _put_dropping_oldest(self, event: TraceEvent) -> None:
        if self._queue.full():
            self._queue.get_nowait()
            self._dropped_count += 1
        self._queue.put_nowait(event)

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def events_ignored(self) -> int:
        """Spans `span_mapping.classify` didn't recognize, or that failed
        to build into a valid `TraceEvent` (HTTP/DB auto-instrumentation
        spans, mostly, plus the rare malformed-attribute span) -- counted
        here, at the source, rather than via `RuntimeChannel`'s usual
        channel-tracked `events_ignored` (bumped on an `Unresolved`
        *NodeIndex* result): these spans are filtered out before ever
        reaching the queue, specifically so they never hit
        `NodeIndex.resolve()` and flood the dead-letter buffer (see the
        task's own "Ignored spans are not dead letters" note). Added to
        the channel-tracked count (a DIFFERENT, complementary measure) in
        `RuntimeChannel._source_statuses_locked`, so `SourceStatus.
        events_ignored` for this method reflects both "never made it past
        classification" and "reached NodeIndex but didn't resolve"."""
        return self._ignored_count

    def _build_app(self) -> Starlette:
        return Starlette(
            routes=[Route("/v1/traces", self._handle_export, methods=["POST"])],
            middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])],
        )

    async def _handle_export(self, request: Request) -> Response:
        """`POST /v1/traces`. Never raises into uvicorn -- a malformed body
        is a 400, not a crash, per `TraceSource`'s "transient hiccup" contract."""
        content_length = request.headers.get("content-length")
        if content_length is not None:
            with contextlib.suppress(ValueError):
                if int(content_length) > _MAX_BODY_BYTES:
                    return Response(status_code=413)

        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _MAX_BODY_BYTES:
                return Response(status_code=413)

        try:
            otlp_request, is_json = self._parse_body(bytes(body), request)
        except _ParseFailure as exc:
            self._parse_error_count += 1
            logger.debug("runtime: malformed OTLP export body: %s", exc)
            return Response(status_code=400)

        for resource_attrs, scope_name, span in _walk_spans(otlp_request):
            try:
                events = _span_to_events(resource_attrs, scope_name, span)
            except _ParseFailure as exc:
                # A span that classified against a rule but then failed to
                # build (e.g. a `code.function.name` attribute ending in
                # "." producing an empty name -- see `span_mapping._make_ref`)
                # must not take the REST of this export's spans down with
                # it: count it the same as an unrecognized span and move on.
                self._ignored_count += 1
                logger.debug("runtime: skipping malformed span %r: %s", span.name, exc)
                continue
            if events is None:
                self._ignored_count += 1
                continue
            self._put_dropping_oldest(events[0])
            self._put_dropping_oldest(events[1])

        response = ExportTraceServiceResponse()
        if is_json:
            return Response(content=json_format.MessageToJson(response), media_type="application/json")
        return Response(content=response.SerializeToString(), media_type="application/x-protobuf")

    def _parse_body(self, raw_body: bytes, request: Request) -> tuple[ExportTraceServiceRequest, bool]:
        """Returns `(request, is_json)` -- `is_json` decides whether the 200
        response is encoded the same way (OTLP JSON vs. protobuf), per the
        task's "Reply 200 ... encoded the same way as the request"."""
        body = raw_body
        if request.headers.get("content-encoding") == "gzip":
            body = _gzip_decompress_bounded(body, _MAX_DECOMPRESSED_BYTES)

        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            return _otlp_json_to_proto(body), True
        return ExportTraceServiceRequest.FromString(body), False
