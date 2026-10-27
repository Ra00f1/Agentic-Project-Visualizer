# simulator

A small, actually-runnable driver for `mock-agent-project`'s seeded `mock_agent`
Mongo database. It does **not** import anything from `mock-agent-project/src/`
— that tree is a static-analysis fixture whose own README says nothing in it
is meant to run (deliberately broken circular imports, dynamic dispatch). This
directory is a separate sibling that emits scripted `apv_trace` events with
the same names/modules the L1 scanner reads out of Mongo, so the visualizer's
runtime overlay has something real to glow at.

Fake behavior only: sleeps and a hand-picked canonical path per workflow, no
LLM calls, no real agent logic.

## Running

From `mock-agent-project/`:

```bash
uv run python -m simulator.ui
```

This opens a ~400×500 window listing every workflow module found in
`simulator/scripts/`. Click "Run" on a workflow to emit its scripted trace
events; only one workflow runs at a time. Check "Trigger error" before
clicking Run to make that workflow's last agent's last tool call raise
instead of returning — the visualizer should show a persistent red ring.

Trace events go to `APV_TRACE_LOG_PATH` if set, otherwise
`apv_trace.default_log_path()` (the same shared path `apv-run` and a plain
`apv_trace.configure()` default to — Task 21's D1; typically
`%LOCALAPPDATA%\apv\apv\Logs\trace\live.jsonl` on Windows) — shown at the
top of the window. Point the visualizer backend's
`AGENTIC_DEFAULT_TRACE_LOG_PATH` at the same file and connect it to
`mock_agent` to watch nodes glow live.

## The manifest

`simulator/manifest.json` maps `"<kind>:<name>"` (e.g. `"agent:Research
Agent"`) to that record's real Mongo `_id`. Every workflow script passes
`name=` values that match this key format, so `apv_trace` can resolve
`ref.db_id` for every emitted event — that's what lets the visualizer's
`NodeIndex` correlate events via its highest-confidence strategy instead of
falling back to typed-name matching.

Build or refresh it after seeding/reseeding `mock_agent`:

```bash
uv run python -m simulator.build_manifest --uri mongodb://localhost:27017 --db mock_agent
```

`manifest.json` is git-ignored — it holds real ObjectIds that differ per
developer's local seed. The UI shows a warning banner at startup if it's
missing; scripts still run without one, just with `ref.db_id=None` on every
event (the resolver falls back to typed-name matching).

**Stale manifest after a reseed** silently dead-letters every event, or falls
back to a lower-confidence resolution strategy — the ObjectIds baked into an
old manifest no longer exist in the reseeded database. Re-run
`build_manifest.py` any time `mock_agent` is reseeded, before running the
simulator.

## Adding a new workflow script

One file per workflow in `simulator/scripts/`, named after the workflow (e.g.
`simulator/scripts/research_and_write.py` for the `research_and_write` Mongo
record). Each file:

1. Declares `WORKFLOW_NAME = "<display name>"` — must match that workflow's
   `display_name` (or `name`, if no `display_name`) in Mongo, since that's the
   key `build_manifest.py` writes into `manifest.json`.
2. Exports `async def run(fail: bool = False) -> None`, decorated
   `@trace(kind="workflow", name=WORKFLOW_NAME)`, that calls the shared
   `simulate_agent(name, tools, duration, fail=...)` helper (from
   `simulator/scripts/__init__.py`) once per agent in the workflow's real
   `agent_ids` order, with that agent's real tool list. Pass `fail=fail` only
   on the last agent's call — `simulate_agent` itself only fails the last
   tool it calls, so a `fail=True` run still shows the whole chain glow
   green before the final red ring.
3. Picks one canonical path through the workflow if the real Mongo record has
   branching agents (delegation edges, sub-agents with no single linear
   order) — this is a demo, not a full decision-tree simulator.

`ui.py` discovers workflows by listing `simulator/scripts/`'s own submodules
at startup — no registry to update elsewhere. A script missing `run` or
`WORKFLOW_NAME` is skipped with a warning printed to stderr rather than
crashing the whole UI.

## Updating `apv_trace`

The tracing helper both this simulator and `runtime-demo-project` depend on
lives at `packages/apv_trace/`, not vendored here — see that package's own
README for why. Change it there; both consumers pick it up on their next
`uv sync` (both depend on it via an editable `[tool.uv.sources]` path
dependency).
