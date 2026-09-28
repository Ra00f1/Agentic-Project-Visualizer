"""Code tools tools — stubs backing the Mongo tool rows."""

from typing import Any

def run_lint(*args: Any, **kwargs: Any) -> dict:
    """Stub for the run_lint tool."""
    return {"tool": "run_lint", "ok": True}

def run_tests(*args: Any, **kwargs: Any) -> dict:
    """Stub for the run_tests tool."""
    return {"tool": "run_tests", "ok": True}

def open_pr(*args: Any, **kwargs: Any) -> dict:
    """Stub for the open_pr tool."""
    return {"tool": "open_pr", "ok": True}

def code_diff(*args: Any, **kwargs: Any) -> dict:
    """Stub for the code_diff tool."""
    return {"tool": "code_diff", "ok": True}

