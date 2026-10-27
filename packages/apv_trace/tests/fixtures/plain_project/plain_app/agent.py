"""A tiny, un-traced "agent": a sync call, an async call, one that raises,
and one that catches -- no `apv_trace`/OTel import anywhere in this fixture.
`apv-run --functions` is what's meant to observe these, not any code here.
"""

from __future__ import annotations

from .tools import classify, lookup


def handle(text: str) -> str:
    category = classify(text)
    articles = lookup(category)
    return articles[0] if articles else "no match"


async def handle_async(text: str) -> str:
    return handle(text)


def handle_and_raise(text: str) -> str:
    if text.strip().upper() == "CRASH":
        raise RuntimeError("simulated failure")
    return handle(text)


def handle_and_catch(text: str) -> str:
    try:
        return handle_and_raise(text)
    except RuntimeError:
        return "recovered"
