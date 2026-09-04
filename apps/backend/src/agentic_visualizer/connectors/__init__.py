"""Database connectors — the "read raw data" layer.

Every connector implements the `Connector` Protocol. Scanners consume the
Protocol, never a concrete class, so adding Postgres/MySQL/pgvector/Qdrant
later is one new module and zero changes anywhere else.
"""

from .base import Connector, CollectionSchema
from .mongo import MongoConnector, close_all_clients, invalidate_schema_cache

__all__ = [
    "Connector",
    "CollectionSchema",
    "MongoConnector",
    "close_all_clients",
    "invalidate_schema_cache",
]
