# apv_trace

Tiny tracing helper for a target agentic project. Ships **inside the target
project** (here, `runtime-demo-project`) — never as a dependency of the
Agentic Project Visualizer app itself. See `.claude/CLAUDE.md` §1 "Runtime
overlay" for why that boundary matters.

## Usage

```python
from pathlib import Path
from apv_trace import configure, trace

configure(Path("trace.jsonl"))  # once, at process start

@trace(kind="tool", name="lookup_kb")
def lookup_kb_tool(topic: str) -> list[str]:
    ...
```

`kind` is one of `"workflow" | "agent" | "tool" | "function"`. `name`
overrides the emitted `ref.name` — pass whatever name your project's own
catalog/DB uses for that entity, so the visualizer's node-resolution step
can match on it directly.

Each call emits a `start` event, then either an `end` event (return) or an
`error` event (exception — the exception still propagates after it's
logged). Nested calls carry a `parent_span_id` so the visualizer can
reconstruct the call tree, not just isolated pulses.

## Where the log goes

By default: `platformdirs.user_log_dir("apv") / "trace" / "<run_id>.jsonl"`.
Override with the `APV_TRACE_LOG_PATH` environment variable to point at a
fixed path (useful for a `wc -l` / `jq` sanity check right after a run).

## Failure mode

Instrumentation must never take down the project it's instrumenting. Every
write failure (disk full, file locked, serialization error) is caught and
swallowed inside `_emit` — you'll see a rate-limited (once/minute) warning on
stderr instead of an exception. If you kill the process mid-run, whatever
was already written stays on disk (writes are unbuffered), and nothing about
the emitter should be visible in your own exception handling.

## ContextVar nesting caveat

Span nesting is tracked via `contextvars.ContextVar`, not `threading.local`,
because `asyncio` tasks share an OS thread and a `ContextVar` is copied
per-task under Python's default task factory (3.11+). If you port this
decorator into a project that runs on an older Python or a custom task
factory that doesn't copy context per task, nested async spans may report
the wrong `parent_span_id`. Not a concern for this repo (3.11+ throughout),
but worth knowing if you lift `apv_trace` into a different project.
