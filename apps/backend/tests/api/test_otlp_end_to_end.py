"""End-to-end test for the OTLP/HTTP trace method (Task 23, Verification
item 9): a REAL OTel SDK subprocess, a REAL `OTLPSpanExporter`, hitting the
REAL loopback receiver -- no fakes anywhere in this pipeline.

No Mongo needed: builds a `Graph` by hand (same pattern as
`tests/runtime/test_channel.py`) and drives `RuntimeChannel` directly,
bypassing the FastAPI app/scanner layer entirely -- this test is about the
subprocess -> HTTP export -> `OtlpHttpTraceSource` -> `span_mapping` ->
`RuntimeStateStore` -> WebSocket-message pipeline, not graph scanning.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from datetime import UTC, datetime

import pytest

from agentic_visualizer.domain import Graph, Node, Provenance
from agentic_visualizer.runtime.channel import MethodConfig, RuntimeChannel
from agentic_visualizer.runtime.messages import DeadLetterStatsMessage, RuntimeMessage
from agentic_visualizer.runtime.methods.otlp_http import OtlpHttpMethod, OtlpHttpSettings

_TS = datetime(2026, 1, 1, tzinfo=UTC)

# Deliberately NOT `apv_trace.configure()` -- plain OTel SDK, exactly what a
# target project with no `apv_trace` dependency would run. Sets the SAME
# `apv.*` attributes `runtime-demo-project`'s OTLP-only demo does, so this
# resolves against the graph the same way the file-tail method would.
_SUBPROCESS_SCRIPT = """
import sys
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

endpoint = sys.argv[1]
provider = TracerProvider(resource=Resource.create({"service.name": "otlp-e2e-test"}))
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
tracer = provider.get_tracer("otlp-e2e-test")
with tracer.start_as_current_span("search") as span:
    span.set_attribute("apv.kind", "tool")
    span.set_attribute("apv.name", "search_docs")
    span.set_attribute("apv.module", "mock_agent.tools")
provider.shutdown()
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_real_otel_sdk_subprocess_reaches_dead_letter_stats_within_10s() -> None:
    port = _free_port()
    node = Node(
        id="mongo:db.tools:1",
        type="tool",
        name="search_docs",
        provenance=Provenance(source="test", source_ref="test:1", scanned_at=_TS),
    )
    channel = RuntimeChannel(
        methods={"otlp_http": OtlpHttpMethod()}, dead_letter_stats_interval_seconds=0.5
    )
    await channel.update_graph(Graph(nodes=[node]))
    await channel.apply_config([MethodConfig(id="otlp_http", settings=OtlpHttpSettings(port=port))])
    subscriber = channel.subscribe()
    await channel.start()
    try:
        endpoint = f"http://127.0.0.1:{port}/v1/traces"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _SUBPROCESS_SCRIPT,
            endpoint,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        assert proc.returncode == 0, stderr.decode()

        async def _wait_for_stats() -> DeadLetterStatsMessage:
            while True:
                message: RuntimeMessage = await subscriber.get()
                if isinstance(message, DeadLetterStatsMessage) and message.events_resolved >= 1:
                    return message

        stats = await asyncio.wait_for(_wait_for_stats(), timeout=10.0)
        # One span -> one `start` TraceEvent + one `end` TraceEvent, both
        # resolve against the one node -- `events_resolved` counts events,
        # not spans.
        assert stats.events_resolved == 2

        snapshot = channel.state_store.snapshot()
        assert len(snapshot) == 1
        assert snapshot[0].node_id == node.id
        assert snapshot[0].status == "idle"  # SimpleSpanProcessor exports only after the span ends
        assert snapshot[0].last_ended_at is not None
    finally:
        channel.unsubscribe(subscriber)
        await channel.stop()
