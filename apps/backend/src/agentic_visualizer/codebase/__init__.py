"""Codebase adapters — the "raw source code -> readable API" layer.

Every code-reading scanner (L2, L3, L4) goes through this Protocol so
switching from a local path to a shallow GitHub clone (or a remote FS
someday) is one new implementation, not a rewrite. Same pluggable-boundary
story as `connectors/` for databases.
"""

from .base import CodebaseAdapter, SymbolLocation
from .local import LocalCodebaseAdapter

__all__ = [
    "CodebaseAdapter",
    "SymbolLocation",
    "LocalCodebaseAdapter",
]
