"""Integration tests for OtlpHttpTraceSource -- real loopback HTTP calls,
real protobuf (de)serialization, no mocks (Task 23 Verification items 1-4).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import json
import socket

import httpx
import pytest
from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

from agentic_visualizer.runtime.sources.otlp_http import OtlpHttpTraceSource
from agentic_visualizer.trace.events import TraceEvent

_START_NS = 1_700_000_000_000_000_000
_END_NS = _START_NS + 250_000_000  # 250ms later


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _kv(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def _make_span(span_id: bytes, name: str, attrs: dict[str, str], *, error: bool = False) -> Span:
    span = Span(
        trace_id=b"\x01" * 16,
        span_id=span_id,
        name=name,
        start_time_unix_nano=_START_NS,
        end_time_unix_nano=_END_NS,
        attributes=[_kv(k, v) for k, v in attrs.items()],
    )
    if error:
        span.status.code = Status.STATUS_CODE_ERROR
        span.status.message = "boom"
        exception_event = span.events.add()
        exception_event.name = "exception"
        exception_event.attributes.extend(
            [
                _kv("exception.type", "ValueError"),
                _kv("exception.message", "boom"),
                _kv("exception.stacktrace", "Traceback ..."),
            ]
        )
    return span


def _build_request(spans: list[Span], *, service_name: str = "test-service") -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    resource_spans.resource.attributes.append(_kv("service.name", service_name))
    scope_spans = resource_spans.scope_spans.add()
    scope_spans.scope.name = "test-scope"
    scope_spans.spans.extend(spans)
    return request


def _three_spans() -> list[Span]:
    """One OpenInference TOOL span, one GenAI execute_tool span, one
    unmapped HTTP span -- per Verification item 1."""
    return [
        _make_span(b"\x01" * 8, "search", {"openinference.span.kind": "TOOL", "tool.name": "web_search"}),
        _make_span(
            b"\x02" * 8, "lookup", {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_order"}
        ),
        _make_span(b"\x03" * 8, "GET /healthz", {"http.method": "GET", "http.route": "/healthz"}),
    ]


async def _drain(source: OtlpHttpTraceSource, count: int, timeout: float = 2.0) -> list[TraceEvent]:
    collected: list[TraceEvent] = []
    gen = source.events()
    try:
        for _ in range(count):
            collected.append(await asyncio.wait_for(gen.__anext__(), timeout=timeout))
    finally:
        await gen.aclose()
    return collected


@pytest.mark.asyncio
async def test_protobuf_export_with_three_spans_produces_four_events_one_ignored() -> None:
    port = _free_port()
    source = OtlpHttpTraceSource(port)
    await source.start()
    try:
        request = _build_request(_three_spans())
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/v1/traces",
                content=request.SerializeToString(),
                headers={"content-type": "application/x-protobuf"},
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/x-protobuf"
        ExportTraceServiceResponse.FromString(resp.content)  # doesn't raise

        events = await _drain(source, 4)
        assert source.events_ignored == 1
        assert {e.span_id for e in events} == {"0101010101010101", "0202020202020202"}
        assert {e.kind for e in events} == {"tool"}
        assert {e.phase for e in events} == {"start", "end"}
        start_events = [e for e in events if e.phase == "start"]
        assert all(e.ts.timestamp() == pytest.approx(_START_NS / 1_000_000_000) for e in start_events)
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_otlp_json_with_hex_ids_gives_identical_events() -> None:
    port_protobuf = _free_port()
    port_json = _free_port()
    source_protobuf = OtlpHttpTraceSource(port_protobuf)
    source_json = OtlpHttpTraceSource(port_json)
    await source_protobuf.start()
    await source_json.start()
    try:
        spans = _three_spans()
        request = _build_request(spans)

        async with httpx.AsyncClient() as client:
            await client.post(
                f"http://127.0.0.1:{port_protobuf}/v1/traces",
                content=request.SerializeToString(),
                headers={"content-type": "application/x-protobuf"},
            )
            json_body = json_format.MessageToJson(request).encode("utf-8")
            # `MessageToJson` base64-encodes bytes fields per the STANDARD
            # protobuf JSON mapping -- re-hex-encode the id fields so this
            # body matches what a real OTLP/HTTP JSON exporter sends (the
            # whole point of `_otlp_json_to_proto`'s own conversion step).
            parsed = json.loads(json_body)

            for resource_spans in parsed.get("resourceSpans", []):
                for scope_spans in resource_spans.get("scopeSpans", []):
                    for span in scope_spans.get("spans", []):
                        for key in ("traceId", "spanId"):
                            if key in span:
                                span[key] = base64.b64decode(span[key]).hex()
            resp = await client.post(
                f"http://127.0.0.1:{port_json}/v1/traces",
                content=json.dumps(parsed).encode("utf-8"),
                headers={"content-type": "application/json"},
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"

        protobuf_events = await _drain(source_protobuf, 4)
        json_events = await _drain(source_json, 4)

        key = lambda e: (e.span_id, e.phase)  # noqa: E731 - local, throwaway sort key
        protobuf_sorted = sorted(protobuf_events, key=key)
        json_sorted = sorted(json_events, key=key)
        for pb_event, json_event in zip(protobuf_sorted, json_sorted, strict=True):
            assert pb_event.span_id == json_event.span_id
            assert pb_event.phase == json_event.phase
            assert pb_event.kind == json_event.kind
            assert pb_event.ts == json_event.ts
    finally:
        await source_protobuf.stop()
        await source_json.stop()


@pytest.mark.asyncio
async def test_gzip_body_is_decompressed() -> None:
    port = _free_port()
    source = OtlpHttpTraceSource(port)
    await source.start()
    try:
        request = _build_request(
            [_make_span(b"\x09" * 8, "search", {"openinference.span.kind": "TOOL", "tool.name": "x"})]
        )
        compressed = gzip.compress(request.SerializeToString())
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/v1/traces",
                content=compressed,
                headers={"content-type": "application/x-protobuf", "content-encoding": "gzip"},
            )
        assert resp.status_code == 200
        events = await _drain(source, 2)
        assert len(events) == 2
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_oversized_body_is_rejected_with_413() -> None:
    port = _free_port()
    source = OtlpHttpTraceSource(port)
    await source.start()
    try:
        oversized = b"\x00" * (9 * 1024 * 1024)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/v1/traces",
                content=oversized,
                headers={"content-type": "application/x-protobuf"},
            )
        assert resp.status_code == 413
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_malformed_body_is_rejected_with_400_and_counted() -> None:
    port = _free_port()
    source = OtlpHttpTraceSource(port)
    await source.start()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/v1/traces",
                content=b"not a protobuf message, deliberately garbage bytes \xff\xfe",
                headers={"content-type": "application/x-protobuf"},
            )
        assert resp.status_code == 400
        assert source.stats().parse_error_count == 1
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_error_status_span_with_exception_event_produces_error_event() -> None:
    port = _free_port()
    source = OtlpHttpTraceSource(port)
    await source.start()
    try:
        request = _build_request(
            [
                _make_span(
                    b"\x0a" * 8,
                    "search",
                    {"openinference.span.kind": "TOOL", "tool.name": "x"},
                    error=True,
                )
            ]
        )
        async with httpx.AsyncClient() as client:
            await client.post(
                f"http://127.0.0.1:{port}/v1/traces",
                content=request.SerializeToString(),
                headers={"content-type": "application/x-protobuf"},
            )
        events = await _drain(source, 2)
        error_event = next(e for e in events if e.phase == "error")
        assert error_event.meta["error"]["type"] == "ValueError"
        assert error_event.meta["error"]["message"] == "boom"
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_error_status_without_exception_event_uses_status_message() -> None:
    port = _free_port()
    source = OtlpHttpTraceSource(port)
    await source.start()
    try:
        span = _make_span(b"\x0b" * 8, "chain-step", {"openinference.span.kind": "TOOL", "tool.name": "x"})
        span.status.code = Status.STATUS_CODE_ERROR
        span.status.message = "no exception event, just a status"
        request = _build_request([span])
        async with httpx.AsyncClient() as client:
            await client.post(
                f"http://127.0.0.1:{port}/v1/traces",
                content=request.SerializeToString(),
                headers={"content-type": "application/x-protobuf"},
            )
        events = await _drain(source, 2)
        error_event = next(e for e in events if e.phase == "error")
        assert error_event.meta["error"]["message"] == "no exception event, just a status"
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_one_malformed_span_does_not_abort_the_rest_of_the_batch() -> None:
    """A span that CLASSIFIES (openinference.span.kind=TOOL) but resolves
    to an empty name (no tool.name, and the span's own `name` field is also
    empty -- a real, if unusual, OTel export) must not take the other,
    valid span in the same export down with it (review finding: this used
    to raise an uncaught pydantic ValidationError out of the route)."""
    port = _free_port()
    source = OtlpHttpTraceSource(port)
    await source.start()
    try:
        malformed = _make_span(b"\x0c" * 8, "", {"openinference.span.kind": "TOOL"})  # no tool.name, no span name
        valid = _make_span(b"\x0d" * 8, "search", {"openinference.span.kind": "TOOL", "tool.name": "web_search"})
        request = _build_request([malformed, valid])
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/v1/traces",
                content=request.SerializeToString(),
                headers={"content-type": "application/x-protobuf"},
            )
        assert resp.status_code == 200  # not a 500 -- the malformed span didn't crash the request
        events = await _drain(source, 2)
        assert {e.span_id for e in events} == {"0d0d0d0d0d0d0d0d"}
        assert source.events_ignored == 1
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_gzip_bomb_is_rejected_not_fully_decompressed_into_memory() -> None:
    """A highly-compressible, small-on-the-wire gzip body that would expand
    far past the body-size cap must be rejected, not decompressed in full."""
    port = _free_port()
    source = OtlpHttpTraceSource(port)
    await source.start()
    try:
        huge_compressible = b"\x00" * (64 * 1024 * 1024)  # 64MB of zeros -- gzips to a few KB
        compressed = gzip.compress(huge_compressible)
        assert len(compressed) < 1024 * 1024  # sanity: genuinely a "small body, huge payload" case
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/v1/traces",
                content=compressed,
                headers={"content-type": "application/x-protobuf", "content-encoding": "gzip"},
            )
        assert resp.status_code == 400
        assert source.stats().parse_error_count == 1
    finally:
        await source.stop()


def test_status_reports_error_when_the_serve_task_dies_unexpectedly() -> None:
    """`_on_serve_task_done` (the done-callback `start()` attaches) must
    turn a listener that died on its own -- not via `stop()`'s cancel --
    into a visible `state="error"`, not a silent "running" forever."""

    async def _boom() -> None:
        raise RuntimeError("simulated listener crash")

    async def _run() -> None:
        source = OtlpHttpTraceSource(_free_port())
        task = asyncio.create_task(_boom())
        with contextlib.suppress(RuntimeError):
            await task
        source._on_serve_task_done(task)  # noqa: SLF001 - exercising the callback directly, see docstring

        status = source.status()
        assert status.state == "error"
        assert "simulated listener crash" in (status.detail or "")

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_port_already_bound_reports_error_state_other_sources_keep_running() -> None:
    port = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        conflicting = OtlpHttpTraceSource(port)
        other = OtlpHttpTraceSource(_free_port())
        await conflicting.start()
        await other.start()
        try:
            status = conflicting.status()
            assert status.state == "error"
            assert str(port) in (status.detail or "")

            assert other.status().state == "running"
        finally:
            await conflicting.stop()
            await other.stop()
    finally:
        blocker.close()
