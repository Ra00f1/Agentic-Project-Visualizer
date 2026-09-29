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
├── apv_trace/              Tracing helper (decorator + emitter). See its own README.
└── runtime_demo/
    ├── workflow.py          @trace(kind="workflow") entry point
    ├── agent.py              @trace(kind="agent") handler, calls a tool + a function
    ├── tools.py                Two @trace(kind="tool", name=...) functions
    ├── functions.py             One @trace(kind="function") internal helper
    └── run_demo.py               `python -m runtime_demo.run_demo`
```

## Running it

```bash
cd runtime-demo-project
uv run python -m runtime_demo.run_demo
```

Runs four scripted support tickets through the workflow — three succeed,
one (`"CRASH"`) deliberately raises to produce an `error` trace event. The
process prints where the log landed, then exits.

## Where the log lands

Default: `platformdirs.user_log_dir("apv") / "trace" / "<run_id>.jsonl"`
(on Windows, typically under `%LOCALAPPDATA%\apv\trace\`). Override with
`APV_TRACE_LOG_PATH` to point at a fixed path:

```bash
APV_TRACE_LOG_PATH=./trace.jsonl uv run python -m runtime_demo.run_demo
wc -l trace.jsonl
jq -c '.' < trace.jsonl | head
```

## Testing "kill mid-run"

Start the demo, then `Ctrl+C` partway through. Because the emitter writes
unbuffered, whatever events were already emitted stay on disk — you should
see a partial log with `start` events that never got a matching `end`, and
no exception should escape from `apv_trace` itself (it swallows every write
failure; a `KeyboardInterrupt` is not a write failure and will still stop
the process normally, which is expected).
