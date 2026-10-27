# runtime-demo-project

A small, **actually-runnable** synthetic agentic project. Exists so the
Agentic Project Visualizer's runtime-tracing overlay has something real to
instrument, tail, and stream — a working workflow → agent → tool → function
call chain, plus a deliberate failure path.

## Why this isn't `mock-agent-project`

`../mock-agent-project` is the golden fixture for the *static* scanners
(L1-L4). Its own README says: *"Nothing here is meant to run. Every file is
a stub whose only job is to exercise a specific shape the visualizer's
scanners must handle."* Large parts of it are deliberately broken (circular
imports, dynamic `importlib` dispatch marked "not a bug, don't fix") to
exercise scanner edge cases. Instrumenting and executing it would either
silently change what that golden fixture means for the existing L1-L3
scanner tests, or require building an unrelated demo-execution path through
code that was built specifically not to need one.

`runtime-demo-project` is the opposite: a tiny, deliberately *working*
project with no DB behind it, built purely to produce a real trace log.

## Layout

```
runtime-demo-project/
└── runtime_demo/
    ├── workflow.py          Workflow entry point — kind "workflow"
    ├── agent.py              TriageAgent.handle — kind "agent", calls a tool + a function
    ├── tools.py                Two tool functions — kind "tool"
    ├── functions.py             One internal helper — kind "function"
    ├── run_demo.py               `python -m runtime_demo.run_demo`, calls `apv_trace.configure()`
    └── run_demo_otel.py           `python -m runtime_demo.run_demo_otel` (Task 23) — plain OTel SDK,
                                     NO apv_trace.configure(), exports via OTLP/HTTP instead of a log file
```

The tracing helper itself lives at `../packages/apv_trace` (extracted there
in task 8 — nothing under `apv_trace/` in this project, that layout is
stale). This project depends on it only for `configure()`; see "How the
demo is instrumented" below for why its own code doesn't use its `@trace`
decorator.

## Running it

```bash
cd runtime-demo-project
uv run python -m runtime_demo.run_demo
```

Runs four scripted support tickets through the workflow — three succeed,
one (`"CRASH"`) deliberately raises to produce an `error` trace event. The
process prints where the log landed, then exits.

## Where the log lands

Default: `apv_trace.default_log_path()` — one shared, stable file per
machine (Task 21's D1), not a fresh one per run. Resolves to
`platformdirs.user_log_dir("apv") / "trace" / "live.jsonl"`; on Windows
that's typically `%LOCALAPPDATA%\apv\apv\Logs\trace\live.jsonl` — print
the real path on your own machine with:

```bash
uv run python -c "import apv_trace; print(apv_trace.default_log_path())"
```

If `APV_TRACE_LOG_PATH` isn't set, `run_demo.py` prints the exact value to
set the backend's `AGENTIC_DEFAULT_TRACE_LOG_PATH` to, since pointing it at
the wrong file is the easiest way to see nothing happen in the visualizer.
Override with `APV_TRACE_LOG_PATH` to point at a fixed path instead:

```bash
APV_TRACE_LOG_PATH=./trace.jsonl uv run python -m runtime_demo.run_demo
wc -l trace.jsonl
jq -c '.' < trace.jsonl | head
```

## How the demo is instrumented

`runtime_demo/` imports only the OpenTelemetry API directly
(`opentelemetry.trace.get_tracer(__name__)`) — not `apv_trace`'s own
`@trace` decorator. This is deliberate (task 14): it's the demo for "a
project that already speaks OTel needs only our attribute convention, not
our decorator." `run_demo.py` is the one place that still imports
`apv_trace`, and only for `configure()`.

Each traced call wraps itself in `with _tracer.start_as_current_span(name,
attributes={...}):`, where the attributes dict always sets:

- `apv.kind` — one of `"workflow" | "agent" | "tool" | "function"`. A span
  missing this is invisible to `apv_trace`'s processor (see
  `packages/apv_trace/README.md`).
- `apv.name` — the name the visualizer's resolver matches against; for the
  two tool functions this intentionally differs from the function name
  (`classify_ticket_tool` emits `apv.name = "classify_ticket"`), the same
  `name=` override the old decorator supported.
- `apv.module` / `apv.qualname` — `__name__` / `fn.__qualname__`, read at
  module-import time into each file's own `_*_ATTRS` constant.

Each module keeps its attrs dict as a plain module-level constant (next to
`_tracer = trace.get_tracer(__name__)`) rather than rebuilding it per call —
nothing in it varies call to call. `tests/test_demo_spans.py` checks every
hand-written `apv.qualname` against the real function object, since nothing
else catches that value going stale on a rename.

## Running it under apv-run (zero-code function tracing)

```bash
cd runtime-demo-project
APV_TRACE_LOG_PATH=./trace.jsonl uv run apv-run --functions --no-bridge -- python -m runtime_demo.run_demo
```

**`--no-bridge` is required here, and `--functions` roughly doubles the
line count.** Every function in `runtime_demo/` already writes its own
raw OTel span (see "How the demo is instrumented" above) — a span opened
*inside* the function body, not by a wrapper that runs before the
function starts. `apv-run --functions`'s `sys.monitoring`-based tracer
can only skip a call it can prove is already reported by checking
whether a matching span is already open at the moment the call begins;
for a raw span opened inside the function, it isn't yet, so every call
gets reported twice — once as its own `workflow`/`agent`/`tool`/
`function`-kind span, once again as a plain `function`-kind event from
`apv-run` itself (`meta.source: "monitoring"`). `--no-bridge` doesn't
stop the double *event*, but it does stop the double *node bridge*: without
it, both the raw tool span AND the function event would separately light
up the same tool node via `NodeIndex`'s bridge strategy.

A full run writes 34 lines from the demo's own instrumentation plus 38
more from `apv-run --functions` (72 total) — verified directly, not
estimated: 3 successful tickets × 5 functions × 2 events, plus the CRASH
ticket's 2 functions × 2 events (start+error, no `normalize_text`/tool
calls — it raises before reaching them), plus one `TriageAgent` class-
body execution event pair and one `_main` event pair, both from import/
startup time. See `packages/apv_trace/README.md`'s "Zero-code tracing
with apv-run" for the full flag list and the `--functions` 3.12+
requirement.

## Running it OTLP-only (no `apv_trace`, Task 23)

```bash
cd runtime-demo-project
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://127.0.0.1:4318"
$env:OTEL_EXPORTER_OTLP_PROTOCOL = "http/protobuf"
$env:OTEL_BSP_SCHEDULE_DELAY = "500"
uv run python -m runtime_demo.run_demo_otel
```

(Exact text from the "OpenTelemetry (OTLP)" method's own setup hint in the
Setup screen — point `OTEL_EXPORTER_OTLP_ENDPOINT` at whatever port that
method is listening on.)

Runs the identical four scripted tickets through the identical
`workflow.py`/`agent.py`/`tools.py`/`functions.py` code `run_demo.py` does
— same 34 events — but `run_demo_otel.py` never calls
`apv_trace.configure()` at all. It sets up its own `TracerProvider` with a
`BatchSpanProcessor(OTLPSpanExporter())` and nothing else, which is exactly
what a target project that has never heard of `apv_trace` would run. This
works because `runtime_demo/`'s own code already writes raw OTel spans
with `apv.*` attributes directly (see "How the demo is instrumented"
above) — `configure()`'s only job, normally, is attaching a SECOND span
processor (the JSONL writer) to the same spans; skip that step and the
exact same spans still get created, they just only go to OTLP instead.

Because `OTLPSpanExporter()`'s default protocol depends on which version
is installed, this project pins `opentelemetry-exporter-otlp-proto-http`
directly (not the protocol-auto-selecting generic `opentelemetry-exporter-otlp`
package) — the HTTP-specific exporter class always speaks OTLP/HTTP,
regardless of what `OTEL_EXPORTER_OTLP_PROTOCOL` would otherwise select.

With no graph loaded in the backend (or against `mock_agent`'s seeded
graph, which has no node named `search_docs`/`classify_ticket`/etc. under
this demo's naming), every event dead-letters — the same "34" the
file-tail method produces, verified by running this against a bare
`OtlpHttpTraceSource` with an empty graph.

## Testing "kill mid-run"

Start the demo, then `Ctrl+C` partway through. Because the emitter writes
unbuffered, whatever events were already emitted stay on disk — you should
see a partial log with `start` events that never got a matching `end`, and
no exception should escape from `apv_trace` itself (it swallows every write
failure; a `KeyboardInterrupt` is not a write failure and will still stop
the process normally, which is expected).
