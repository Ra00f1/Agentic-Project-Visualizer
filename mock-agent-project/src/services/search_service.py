"""Sync search service — middle of the handler→service→repo chain.

The endpoint `api.routes.list_documents` calls `search(...)`; `search(...)`
calls `default_repo.fetch_all(...)`. That's the canonical chain the L3 scanner
should stitch together end-to-end.
"""

from __future__ import annotations

from typing import Any

from ..repositories.document_repo import default_repo


def search(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Run a keyword search across stored documents."""
    # Delegates straight to the repo — no filtering here in the mock.
    return default_repo.fetch_all(limit=limit)


def search_by_tag(tag: str) -> list[dict[str, Any]]:
    """Return every document carrying `tag`."""
    all_docs = default_repo.fetch_all(limit=1000)
    return [d for d in all_docs if tag in d.get("tags", [])]
