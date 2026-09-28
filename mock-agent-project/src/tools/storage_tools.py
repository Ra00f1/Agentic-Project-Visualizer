"""Storage tools tools — stubs backing the Mongo tool rows."""

from typing import Any

def upload_to_s3(*args: Any, **kwargs: Any) -> dict:
    """Stub for the upload_to_s3 tool."""
    return {"tool": "upload_to_s3", "ok": True}

def download_from_s3(*args: Any, **kwargs: Any) -> dict:
    """Stub for the download_from_s3 tool."""
    return {"tool": "download_from_s3", "ok": True}

def list_bucket(*args: Any, **kwargs: Any) -> dict:
    """Stub for the list_bucket tool."""
    return {"tool": "list_bucket", "ok": True}

