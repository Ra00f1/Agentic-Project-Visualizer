"""Repository layer — bottom of the handler→service→repo call chain.

Kept intentionally boring: the point is to give the L3 call-graph scanner a
clear leaf-node target. If new call edges start terminating elsewhere by
accident, this is where we'd notice.
"""

from __future__ import annotations

from typing import Any


class DocumentRepository:
    """A mock repository. In a real project this would wrap a DB client."""

    def __init__(self, collection: str = "documents") -> None:
        self.collection = collection

    def fetch_all(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return every document in the collection, up to `limit`."""
        return []  # mock

    def fetch_by_id(self, doc_id: str) -> dict[str, Any] | None:
        """Return one document by id, or None if not found."""
        return None  # mock

    def upsert(self, doc: dict[str, Any]) -> str:
        """Insert or update `doc`. Returns the doc id."""
        return doc.get("_id", "mock-id")  # mock


# Module-level singleton — a common enough pattern that the scanner should
# recognize it and NOT double-count method calls made through the alias.
default_repo = DocumentRepository()
