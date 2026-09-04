"""Async LLM service.

Two things to exercise here:
  1. `async def` — the scanner must treat coroutine defs like regular defs.
  2. `asyncio.TaskGroup` fan-out — the scanner should record concurrent calls
     to `_complete_one` as edges from `complete_many`, once per call site.

There is NO real LLM client here (per project rules — this app is a static
visualizer, not an AI product). The name of this file is deliberate: it should
appear as `services/llm_service.py` in the graph even though no inference
happens.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def _complete_one(prompt: str, *, model: str) -> str:
    """Mock single-completion coroutine."""
    return f"[mock:{model}] {prompt[:32]}"


async def complete(prompt: str, *, model: str = "mock-model") -> str:
    """Run a single completion. Sequential entry point for agents."""
    return await _complete_one(prompt, model=model)


async def complete_many(prompts: list[str], *, model: str = "mock-model") -> list[str]:
    """Run several completions concurrently via TaskGroup.

    The scanner should attribute the call to `_complete_one` to this function,
    not lose it inside the TaskGroup context manager.
    """
    results: list[str] = []
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(_complete_one(p, model=model)) for p in prompts]
    for t in tasks:
        results.append(t.result())
    return results


async def stream_completion(prompt: str, *, model: str = "mock-model") -> Any:
    """Mock streaming completion — yields chunks."""
    yield f"[mock:{model}] chunk-1"
    yield f"[mock:{model}] chunk-2"
