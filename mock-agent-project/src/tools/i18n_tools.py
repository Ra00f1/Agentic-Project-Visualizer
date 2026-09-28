"""I18n tools tools — stubs backing the Mongo tool rows."""

from typing import Any

def translate_text(*args: Any, **kwargs: Any) -> dict:
    """Stub for the translate_text tool."""
    return {"tool": "translate_text", "ok": True}

def detect_language(*args: Any, **kwargs: Any) -> dict:
    """Stub for the detect_language tool."""
    return {"tool": "detect_language", "ok": True}

