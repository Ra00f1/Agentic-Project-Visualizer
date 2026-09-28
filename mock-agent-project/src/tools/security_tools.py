"""Security tools tools — stubs backing the Mongo tool rows."""

from typing import Any

def scan_vulnerabilities(*args: Any, **kwargs: Any) -> dict:
    """Stub for the scan_vulnerabilities tool."""
    return {"tool": "scan_vulnerabilities", "ok": True}

def rotate_secret(*args: Any, **kwargs: Any) -> dict:
    """Stub for the rotate_secret tool."""
    return {"tool": "rotate_secret", "ok": True}

def audit_permissions(*args: Any, **kwargs: Any) -> dict:
    """Stub for the audit_permissions tool."""
    return {"tool": "audit_permissions", "ok": True}

