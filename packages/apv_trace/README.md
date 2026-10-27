# apv_trace

Tiny tracing helper for a target agentic project. Ships **inside the target
project** as a dependency — never as a dependency of the Agentic Project
Visualizer app itself. See `.claude/CLAUDE.md` §1 "Runtime overlay" for why
that boundary matters.

Two consumers today: `runtime-demo-project` (a minimal, no-DB fixture) and
`mock-agent-project/simulator` (a scripted simulator driving the seeded
`mock_agent` Mongo DB's real names/ids). Extracted here (task 8) instead of
vendored in each, so a feature added for one — this package's manifest
support started as exactly that — doesn't drift out of sync with the other.

**Built on the OpenTelemetry SDK (task 13), writing our own JSONL, not
OTLP.** `configure()` attaches a `SpanProcessor` to whatever `TracerProvider`
is already globally set (a target project's own instrumentation, if it got
there first) or creates one if none exists yet — see "Cooperating with a
target project's own OTel setup" below. The on-disk shape (`TraceEvent`
JSONL) is unchanged; this only replaced the internals that produce it.

**Package layout.** `__init__.py` holds the public API (`configure`,
`trace`, `TraceKind`) and process-wide state. `_processor.py` (task 17)
holds the `SpanProcessor` that turns spans into JSONL lines.
`_openinference.py` (task 17) holds the OpenInference kind-classification
rules described below. `run.py`, `_bootstrap.py`, `_autoload/`, and
`_monitor.py` (task 18) are the `apv-run` zero-code launcher — see "Zero-
code tracing with apv-run" below. Nothing outside this package imports
from the underscore-prefixed modules directly.

## Usage

```python
from pathlib import Path
from apv_trace import configure, trace

configure(Path("trace.jsonl"))  # once, at process start

@trace(kind="tool", name="lookup_kb")
def lookup_kb_tool(topic: str) -> list[str]:
    ...
```

`kind` is one of `"workflow" | "agent" | "tool" | "function" | "model"`
(`"model"` added in task 17, for OpenInference LLM spans below — a
`@trace`-decorated function can use it too, for code that calls a model
API directly). `name`
overrides the emitted `ref.name` — pass whatever name your project's own
catalog/DB uses for that entity, so the visualizer's node-resolution step
can match on it directly.

Each call emits a `start` event, then either an `end` event (return) or an
`error` event (exception — the exception still propagates after it's
logged). Nested calls carry a `parent_span_id` so the visualizer can
reconstruct the call tree, not just isolated pulses.

## One function, many entities: the `name` parameter shortcut

If you omit `name=` on the decorator AND the decorated function itself has a
parameter literally called `name`, `ref.name` is read from that parameter's
value at **call time** instead of from `fn.__name__`. This is for a generic
helper that stands in for many different catalog entities:

```python
@trace(kind="agent")
async def simulate_agent(name: str, tools: list[str], duration: float) -> None:
    ...

await simulate_agent("Research Agent", tools=["web_search"], duration=1.2)
await simulate_agent("Writer Agent", tools=["document_query"], duration=0.8)
```

One function definition, two distinct trace identities — see
`mock-agent-project/simulator/scripts/` for the pattern this exists for.
Don't combine this with `name=` on the decorator itself: an explicit `name=`
always wins and the function's `name` argument (if any) is ignored for
trace-identity purposes.

## db_id resolution via a manifest

Pass `manifest_path` to `configure()` to populate `ref.db_id` (and
`ref.framework_id`) from a JSON file shaped
`{"<kind>:<name>": {"db_id": "...", "framework_id": "..."}}`:

```python
configure(Path("trace.jsonl"), manifest_path=Path("manifest.json"))
```

Without a manifest (or an entry missing for a given kind+name), both fields
stay `None` and the visualizer's resolver falls back to its lower-confidence
typed-name/tool_fn strategies — tracing still works, just without the
high-confidence `db_id` match path a real target project's DB-backed catalog
would normally provide. `mock-agent-project/simulator/build_manifest.py`
is the reference implementation of building one from real Mongo records.

## Using an OpenInference instrumentor

A project already instrumented with an [OpenInference](https://github.com/Arize-ai/openinference)
library (LangChain, LlamaIndex, Anthropic, OpenAI, ...) needs no `@trace`
decorators at all — `configure()` plus the instrumentor's own call is
enough:

```python
from pathlib import Path
from apv_trace import configure
from openinference.instrumentation.langchain import LangChainInstrumentor
from opentelemetry import trace

configure(Path("trace.jsonl"))  # attaches our processor to the global provider first
LangChainInstrumentor().instrument(tracer_provider=trace.get_tracer_provider())
```

Install the instrumentor package for whichever framework you use
(`pip install openinference-instrumentation-langchain`, `-llama-index`,
`-anthropic`, `-openai`, ...) — none of them are a dependency of
`apv_trace` itself.

**Kind mapping.** Spans are classified by their own `openinference.span.kind`
attribute, not `apv.kind`. The default map: `TOOL` → `tool`, `AGENT` →
`agent`, `LLM` → `model`. Every other OpenInference kind (`CHAIN`,
`RETRIEVER`, `EMBEDDING`, `RERANKER`, `GUARDRAIL`, `EVALUATOR`, `PROMPT`,
`UNKNOWN`, and future values) is skipped — most `CHAIN` spans are LangChain
plumbing (`RunnableSequence`, prompt templates) that would otherwise flood
the dead-letter buffer with names that never resolve. Override the map with
`configure(..., openinference_kinds={"CHAIN": "workflow", "RETRIEVER": None})`
or the `APV_TRACE_OPENINFERENCE_KINDS="CHAIN=workflow,RETRIEVER="`
environment variable (kwarg beats env beats default; an empty value means
"skip"). Keys are case-insensitive; a malformed entry is dropped with one
warning, the rest still apply.

**No live glow for most OpenInference spans.** Many instrumentors (LangChain's
among them) only set `openinference.span.kind` and the span's name attribute
right before the span ends, not when it starts — there is no SDK hook that
fires when an attribute is *set*, only when the span starts or ends. A span
like that can't be written to the log until it's already over, so it never
appears `active` in the runtime overlay while running; it still reports its
duration, status, and any error once finished. A `@trace`-decorated function
always glows live, because `apv.kind` is set before the call begins.

**Don't `@trace` a function a framework already instruments.** A LangChain
tool wrapped in `@trace(kind="tool", ...)` as well would log two runs per
call — one from each — and the runtime overlay would count it twice.

## Zero-code tracing with apv-run

`apv-run` traces a target with **no code change at all** — no
`configure()` call, no `@trace` decorator, no `.instrument()` call
anywhere in the target's own source:

```bash
apv-run -- python -m myapp.main
# or: apv-run --functions --no-bridge -- uvicorn myapp.asgi:app
```

It builds a child environment (log path, manifest, OpenInference kind
overrides, privacy defaults) and prepends an internal directory to
`PYTHONPATH` whose `sitecustomize.py` the interpreter loads automatically
at startup. That shim configures `apv_trace`, auto-discovers and
instruments every installed OpenInference entry point (skipping, not
erroring on, a framework that isn't installed), and — with `--functions`
— starts a `sys.monitoring`-based function tracer. `PYTHONPATH` is
inherited by every child process, so `uvicorn --reload` or
`multiprocessing` workers get traced too; a `runpy`-based in-process
launcher couldn't do either.

**Flags:** `--log PATH`, `--manifest PATH`, `--oi-kinds "CHAIN=workflow,..."`,
`--functions [ROOT]` (default root: cwd), `--max-calls N` (default
10000), `--no-bridge`, `--disable a,b`, `--no-openinference`,
`--show-inputs`.

**`--functions` needs Python 3.12+.** It's built on `sys.monitoring`
(PEP 669) — fast enough for dev use (one callback per call, disabled
per-code-object for every non-project function after its first call) in a
way `sys.setprofile` isn't. There's no fallback for 3.11; without 3.12,
`--functions` prints a warning and the target still runs, untraced on
that one axis.

**`--no-bridge` and double-counting.** `--functions` reports every
*plain* project function call — including one that's ALSO reported by a
`@trace`/OpenInference-traced call wrapping it, UNLESS that wrapper opens
its span before calling the function (true for `@trace` and for most
OpenInference instrumentors, never true for a raw OTel span opened
*inside* the function body — see `runtime-demo-project/README.md` for a
real example of the latter). Where that overlap exists, `NodeIndex`'s
function→tool bridge would otherwise light up the SAME tool node twice;
`--no-bridge` tags every function event `meta.bridge: false` so the
bridge is skipped and only the plain function node lights up.

**A target that also calls `configure()` itself.** `apv-run`'s own
bootstrap already configured `apv_trace` before the target's first line
runs; if the target calls `configure()` again anyway (its own log-path
rotation, or a demo script built to run with or without `apv-run`), that
second call is rotation as designed — OTel spans created after it
correctly follow the new file. The function tracer keeps working too
(it re-resolves the current processor on every write, not the one it
started with) but keeps the `run_id` it was given at startup, so its own
events may carry a different `run_id` than spans created after the
later reconfigure. Found by actually running `apv-run --functions`
against `runtime-demo-project` (which does exactly this) while building
this feature, not assumed.

**`-I`, `-E`, `-S` disable autoload.** Each of these interpreter flags
either ignores `PYTHONPATH` or skips site customization entirely, so
nothing bootstraps — there is no workaround, by design (those flags exist
specifically to produce a clean, unmodified interpreter).

**Windows vs POSIX.** On POSIX, `apv-run` `exec`s the target directly — it
keeps the same PID and gets signals (Ctrl+C) itself. Windows has no
equivalent that preserves the parent's PID, so the target runs as a real
child process instead; `apv-run` ignores `SIGINT` while waiting so Ctrl+C
reaches the child, not the launcher, and exits with the child's own exit
code either way.

## Where the log goes

By default: `apv_trace.default_log_path()`, which resolves to
`platformdirs.user_log_dir("apv") / "trace" / "live.jsonl"` -- one shared,
stable file per machine, not a fresh one per run (Task 21's D1; the
simulator and `apv-run` default to this same path). On Windows that's
typically `%LOCALAPPDATA%\apv\apv\Logs\trace\live.jsonl` -- print the real
path on your own machine with:

```bash
uv run python -c "import apv_trace; print(apv_trace.default_log_path())"
```

Override with the `APV_TRACE_LOG_PATH` environment variable to point at a
fixed path instead (useful for a `wc -l` / `jq` sanity check right after a
run, or to keep one run's events separate from another's).

## Failure mode

Instrumentation must never take down the project it's instrumenting. Every
write failure (disk full, file locked, serialization error) is caught and
swallowed inside `_JsonlSpanProcessor.on_start`/`on_end` — you'll see a
rate-limited (once/minute) warning on stderr instead of an exception. If you
kill the process mid-run, whatever was already written stays on disk
(writes are unbuffered), and nothing about the emitter should be visible in
your own exception handling. A missing or malformed manifest is the same
story: a rate-limited warning, `db_id`/`framework_id` stay unset, tracing
continues. **Exceptions inside a `@trace`-d call are the one exception to
"swallowed"**: `start_as_current_span`'s own defaults record the exception
onto the span (which becomes the `error` event) and then re-raise it to your
caller — tracing observes failures, it never hides them from your code.

## The `apv.*` span attribute convention

Every `@trace`-d call becomes an OTel span carrying `apv.kind`, `apv.name`,
`apv.module`, `apv.qualname` (always set), and `apv.db_id` / `apv.framework_id`
(set only when the manifest resolves them). **A span without `apv.kind` is
invisible to this package's own processor** — if your project (or an
auto-instrumentation agent) shares the same `TracerProvider` for its own
spans (an HTTP client, a DB driver), those spans are silently skipped here,
not written to the JSONL file. That's intentional: this processor only
speaks for `@trace`-d calls.

Span ids are 16-character lowercase hex (OTel's native format), not ULIDs —
if you ever hand-parse the log file instead of going through `TraceEvent`,
don't assume the old ULID shape.

## Cooperating with a target project's own OTel setup

If your project already calls `opentelemetry.trace.set_tracer_provider(...)`
before `apv_trace.configure()` runs — your own manual instrumentation, or an
auto-instrumentation agent — `configure()` detects the existing SDK
`TracerProvider` and attaches its processor to it instead of creating a new
one. OTel's `set_tracer_provider` only succeeds once per process (every
later call anywhere is silently ignored by the SDK itself), so attaching is
the only cooperative option, and it's correct either way: your other
processors/exporters keep running alongside ours. Calling `configure()`
again later in the SAME process (e.g. to rotate the log path) works the
same way — it shuts down its own previous processor first, so nothing is
written twice.

## Context propagation and its limits

Span nesting (and therefore `parent_span_id` chaining) rides OTel's own
`contextvars`-based `Context`, which — like the hand-rolled `ContextVar`
this package used before the OTel rewrite — is copied per-`asyncio.Task`
under Python's default task factory (3.11+), so concurrent `await`-ing
siblings each get the right parent and never cross-contaminate each other's.
It is **not** copied across a `threading.Thread` boundary — a traced call
handed off to a thread pool loses its parent link (see "Known limitations"
below).

## When the overlay works well

This works best in a single-process target project, decorating agent/tool/
workflow functions with `@trace(kind=..., name=<exact DB record name>)`,
writing to a trace file with nothing else interleaved into it. The backend's
file-tailer tolerates OTHER output mixed into the same file (stdlib
`logging`, `print()`) — it cheaply pre-filters and silently skips lines that
don't start `{"v":1...` before even attempting to parse them — but
correctness and the richest resolution path (`ref.db_id` via a manifest, or
`ref.framework_id`) assume a dedicated trace file written only by this
package.

**Agent/workflow decorators must pass `name=<exact DB record name>`.** Unlike
`function`/`tool` nodes (which the resolver can match via `module`+`qualname`
or an `implements` edge back to code), an agent or workflow node generally
has no corresponding source function the static scanner saw — the ONLY way
the resolver can match one is `ref.name` against that record's name in the
DB (the `typed_name` strategy), or `ref.db_id` via a manifest. Get the name
wrong (a typo, a stale name after a DB rename) and that node's events
dead-letter silently.

## Known limitations (not fixed here, by design)

- **Multi-process targets.** One `configure()` call opens one file handle;
  two processes writing to the SAME path will interleave lines mid-write at
  high volume (no file locking). Plan: per-pid log files, a future task.
- **Thread-based concurrency breaks `parent_span_id` chaining.** Span
  nesting rides `contextvars.ContextVar`, which is copied per-`asyncio.Task`
  automatically but NOT per-`threading.Thread` — a traced call handed off to
  a thread pool loses its parent link. Plan: a `copy_context_to_thread()`
  helper, a future task (better specced after seeing which thread-pool
  patterns real target projects actually use).
- **Deduplication when a function is both `@trace`-d and framework-traced.**
  Documented above under "Don't `@trace` a function a framework already
  instruments" — not fixed, since detecting the overlap would mean this
  package inspecting the target project's call graph, which it has no way
  to do from inside a span processor.
- **`apv-run --functions`: no Python 3.11 fallback, no per-process log
  files, no thread-pool parent links either** (the same `contextvars`
  limitation above applies to function tracing too). Closing a bare
  generator (no `try`/`finally` around its `yield`s) writes no `end`
  event on Python 3.13 — confirmed empirically: `PY_UNWIND` fires for
  that case on 3.12 but not on 3.13, apparently because CPython can tear
  down a frame with no exception-table entry without going through the
  instrumented unwind path. A generator with any cleanup code around its
  `yield`s (the common case) is unaffected on both versions.
- **A function both `@trace`-d/OpenInference-traced AND seen by
  `apv-run --functions`** counts two runs per call unless the wrapping
  span opens before the function body does (true for `@trace`, usually
  true for OpenInference, never true for a raw span opened inside the
  function) — see "Zero-code tracing with apv-run" above for the
  `--no-bridge` mitigation, and `runtime-demo-project/README.md` for a
  real example where it applies.
- **No OTLP exporter or receiver, no `opentelemetry-instrument` launcher.**
  This package only ever writes its own `TraceEvent` JSONL (see "Built on
  the OpenTelemetry SDK" above for why) and never makes a network call —
  no exporters are attached to the `TracerProvider` it creates. If you need
  OTLP too, attach your own exporter to the same provider; this package
  won't conflict with it.
- **No writer-side log rotation.** `apv_trace` just appends. If you need
  rotation, point an external rotator (logrotate, a `copytruncate`-style
  tool) at the file — the backend's file-tailer already recovers from both
  rename-based rotation and in-place truncation.
- **Synchronous, unbuffered, blocking writes.** Every event is its own
  blocking `write()` syscall (see `configure`'s own docstring on why —
  durability over throughput). Fine at demo/fixture call volumes; a
  high-throughput target project would need batching, which isn't here.

## Updating this package

Both consumers (`runtime-demo-project`, `mock-agent-project/simulator`)
depend on this via a `[tool.uv.sources]` path dependency — there is no
vendored copy anywhere else to keep in sync. Change it here, `uv sync` in
whichever consumer you're testing against, done.
