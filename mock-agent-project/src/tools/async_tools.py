"""Async-def tools. L2 must resolve `async def` the same as `def`.

Also demonstrates awaiting inside a tool body — the AST walker should
not descend into runtime effects, only names.
"""

import asyncio
from typing import Any


async def async_fetch_url(url: str) -> dict:
    """Fake async HTTP GET."""
    await asyncio.sleep(0)  # never actually runs
    return {"url": url, "status": 200}


async def async_batch_query(ids: list) -> list:
    """Fake async batched DB read."""
    await asyncio.sleep(0)
    return [{"id": i, "data": None} for i in ids]
