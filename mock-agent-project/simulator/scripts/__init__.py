"""One module per workflow the simulator can run — `ui.py` discovers them by
listing this package's own submodules (see `discover_workflows` in `ui.py`).

Every workflow module exports `async def run(fail: bool = False) -> None`.
Setting `fail=True` makes the LAST agent's LAST tool call raise instead of
returning — the "Trigger error" checkbox in the UI. Simplest possible
"script decides which" per this task's own Scope: rather than modeling a
workflow's actual delegation/branching, each script is a flat sequence of
`simulate_agent()` calls that mirrors the corresponding Mongo workflow
record's `agent_ids` order.

`simulate_agent`/`simulate_tool` below are the ONE function definition each
that stands in for every agent/tool any workflow script names — see
`apv_trace`'s README on the `name`-parameter call-time override this relies
on. Shared here (not duplicated per workflow file) because the scaffolding
truly is workflow-agnostic, per this task's own Design section.
"""

from __future__ import annotations

import asyncio
import random

from apv_trace import trace


@trace(kind="agent")
async def simulate_agent(
    name: str,
    tools: list[str],
    duration: float | None = None,
    *,
    fail: bool = False,
    tool_duration_range: tuple[float, float] | None = None,
) -> None:
    """Emit a `start`/`end` (or `error`) span for agent `name`, calling
    `simulate_tool` for each of its tools in turn.

    Exactly one of `duration`/`tool_duration_range` must be given. With
    `duration`, it's split evenly across `tools` so a wider tool fan-out
    doesn't make the whole agent-level pulse proportionally longer -- each
    individual tool glow stays readable regardless of how many tools this
    agent has. With `tool_duration_range` (a recording-friendly mode, e.g.
    for a video/gif), each tool instead gets its OWN independently random
    duration from that `(min, max)` range, so steps visibly vary in length
    rather than ticking by on a uniform beat.

    `fail=True` makes only the LAST tool call raise — the preceding tools
    still complete normally, so the visualizer shows the whole chain
    cascading green before the final red ring, not an immediate failure.
    """
    if tool_duration_range is not None:
        if duration is not None:
            raise ValueError("pass exactly one of duration or tool_duration_range")
        low, high = tool_duration_range
        count = len(tools) or 1
        tool_durations = [random.uniform(low, high) for _ in range(count)]
    elif duration is not None:
        count = len(tools) or 1
        tool_durations = [duration / count] * count
    else:
        raise ValueError("pass exactly one of duration or tool_duration_range")

    if not tools:
        await asyncio.sleep(tool_durations[0])
        if fail:
            raise RuntimeError(f"simulated failure in agent '{name}' (no tools to fail on)")
        return

    for index, (tool_name, tool_duration) in enumerate(zip(tools, tool_durations, strict=True)):
        is_last = index == len(tools) - 1
        await simulate_tool(tool_name, duration=tool_duration, fail=fail and is_last)


@trace(kind="tool")
async def simulate_tool(name: str, duration: float, *, fail: bool = False) -> None:
    """Emit a `start`/`end` (or `error`) span for tool `name`. `duration` is
    how long this tool's glow stays visible before the next event — tuned
    per-script so the visualizer's cascading glow is readable (see each
    workflow script's own `duration` choices), not a fixed constant here.
    """
    await asyncio.sleep(duration)
    if fail:
        raise RuntimeError(f"simulated failure in tool '{name}'")
