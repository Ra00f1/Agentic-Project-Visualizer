"""Nlp tools tools — stubs backing the Mongo tool rows."""

from typing import Any

def classify_sentiment(*args: Any, **kwargs: Any) -> dict:
    """Stub for the classify_sentiment tool."""
    return {"tool": "classify_sentiment", "ok": True}

def extract_entities(*args: Any, **kwargs: Any) -> dict:
    """Stub for the extract_entities tool."""
    return {"tool": "extract_entities", "ok": True}

def score_toxicity(*args: Any, **kwargs: Any) -> dict:
    """Stub for the score_toxicity tool."""
    return {"tool": "score_toxicity", "ok": True}

