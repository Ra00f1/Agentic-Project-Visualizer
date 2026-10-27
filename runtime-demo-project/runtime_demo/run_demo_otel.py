"""Entry point: `python -m runtime_demo.run_demo_otel`.

Task 23's OTLP-only demo: runs the SAME scripted tickets through the SAME
workflow as `run_demo.py`, but with NO `apv_trace.configure()` anywhere in
the process -- a plain OTel SDK `TracerProvider` + the standard
`OTLPSpanExporter` is the only tracing setup here, matching exactly what a
target project that already speaks OpenTelemetry (and has never heard of
`apv_trace`) would run.

This works at all because `runtime_demo/`'s own code (`workflow.py`/
`agent.py`/`tools.py`/`functions.py`) already writes raw OTel spans with
`apv.*` attributes directly -- see README.md's "How the demo is
instrumented." `apv_trace.configure()`'s only job is attaching a SECOND
span processor (the JSONL writer) to whatever `TracerProvider` is globally
set; skip that step entirely and the exact same spans still get created,
they just go wherever THIS script's own processor sends them instead.

`OTLPSpanExporter()` here takes NO explicit `endpoint` -- it resolves
`OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_EXPORTER_OTLP_PROTOCOL` from the
environment, exactly the two vars the "OpenTelemetry (OTLP)" method's own
setup hint tells you to set (see `apps/backend/.../methods/otlp_http.py`).
A `BatchSpanProcessor`, not `SimpleSpanProcessor`: this is the realistic
"a target project's default exporter setup" case the method's own
Context note describes (batched, ~0.5-5s after a call finishes) -- the
backend's end-to-end test uses `SimpleSpanProcessor` instead, deliberately,
to avoid a real batch-delay wait in the test suite.
"""

from __future__ import annotations

import asyncio

from opentelemetry import trace as trace_api
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .workflow import run_support_ticket_workflow

_SCRIPTED_TICKETS = [
    "My card was charged twice, please refund me.",
    "I keep getting an error when I export my report.",
    "How do I get started with the product?",
    "CRASH",  # deliberate failure path -- see agent.py
]


async def _main() -> None:
    # Set BEFORE any span is created, not before this module is imported:
    # `trace_api.get_tracer(...)` calls elsewhere in `runtime_demo/` already
    # ran at THEIR module import time and got back a `ProxyTracer`, which
    # transparently starts delegating to whatever REAL provider is active
    # the moment a span actually starts -- see `apv_trace.__init__`'s own
    # `_EmitterState.tracer` docstring for the same mechanism in play there.
    provider = TracerProvider(resource=Resource.create({"service.name": "runtime-demo-otel"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace_api.set_tracer_provider(provider)

    print("[runtime-demo-otel] exporting via OTLP/HTTP (OTEL_EXPORTER_OTLP_ENDPOINT from the environment)")

    for ticket_text in _SCRIPTED_TICKETS:
        try:
            resolution = await run_support_ticket_workflow(ticket_text)
            print(f"[runtime-demo-otel] {ticket_text!r} -> {resolution}")
        except Exception as exc:  # noqa: BLE001 - demo keeps going past the deliberate CRASH ticket
            print(f"[runtime-demo-otel] {ticket_text!r} -> raised {exc.__class__.__name__}: {exc}")

    # Flushes the batch processor's queue -- without this, a short-lived
    # process can exit before the background export thread ever sends
    # anything, which would look exactly like a silently-misconfigured
    # endpoint instead of what it actually is.
    provider.shutdown()
    print("[runtime-demo-otel] done. Spans flushed to the OTLP endpoint.")


if __name__ == "__main__":
    asyncio.run(_main())
