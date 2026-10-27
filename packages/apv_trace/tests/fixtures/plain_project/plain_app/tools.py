"""Plain functions, no tracing code at all -- `apv-run --functions` is what's
meant to light these up, not any import here."""

from __future__ import annotations


def classify(text: str) -> str:
    return "greeting" if "hello" in text.lower() else "other"


def lookup(category: str) -> list[str]:
    return {"greeting": ["Hi there!"], "other": ["I don't know that one."]}.get(category, [])
