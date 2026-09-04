"""MongoConnector — motor-backed implementation of the Connector Protocol.

Design decisions:

* **URI and DB name are constructor arguments, not module-level state.** The
  visualizer is meant to point at a different DB every time the user asks
  it to (see CLAUDE.md §3, "URIs and target schemas are user-editable at
  runtime; do not cache them into module-level state"). Every connector
  instance is scoped to one connection.

* **Schema introspection samples the first N documents per collection.**
  Mongo is schemaless — there's no authoritative schema to read. Sampling is
  a bounded, deterministic approximation. N is small (default 20) because
  the goal is only to populate the "which fields does this collection have?"
  picker, not to build an exhaustive schema.

* **`stream_rows` doesn't cast `_id` for you.** The connector stays
  connector-native (raw BSON); the L1 scanner does the `ObjectId → str`
  normalization it needs. That way a scanner that wants the raw ObjectId
  (e.g. for cross-referencing in a later analysis pass) isn't fighting the
  connector.

* **`close` is idempotent.** motor's `AsyncIOMotorClient.close()` is safe
  to call twice, but we guard with a flag anyway to make the contract
  explicit.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore[import-untyped]

from .base import CollectionSchema

# --- Process-wide caches --------------------------------------------------
#
# The visualizer opens a fresh MongoConnector per HTTP request. Motor's
# AsyncIOMotorClient is *designed* to be long-lived — it does its own
# connection pooling, topology discovery, and monitoring. Creating one per
# request throws all of that away and pays TLS + topology cost on every
# Refresh click.
#
# So we cache motor clients per URI at module scope. MongoConnector
# instances become cheap wrappers over the shared client. `close()` on the
# connector no longer closes the client (that'd break every other request
# on the same URI); instead, closing the pool is the sidecar's
# shutdown-hook responsibility.
#
# Schema introspection has a similar shape: `introspect_schema` is called
# from both the /schema endpoint AND from the L1 scanner's lazy-collection
# auto-detection on every /graph. Caching per (uri, db) with a short TTL
# avoids running list_collections + sample-20 for each collection twice on
# every Refresh. TTL is short so a mid-session Mongo change is picked up
# reasonably quickly.
# Cache key is (uri, loop_id) — see `_get_or_create_client` for why.
_CLIENT_CACHE: dict[tuple[str, int], AsyncIOMotorClient] = {}
_SCHEMA_CACHE: dict[tuple[str, str], tuple[float, list[CollectionSchema]]] = {}
_SCHEMA_TTL_SECONDS: float = 60.0


def _get_or_create_client(
    uri: str,
    *,
    server_selection_timeout_ms: int,
) -> AsyncIOMotorClient:
    """Return the cached motor client for `uri`, creating one if missing.

    Keyed on `(uri, id(current_event_loop))`. Motor binds its executor
    to the event loop that was active when the client was constructed;
    reusing a client on a different loop raises "Event loop is closed"
    when the original loop has been torn down. In production the sidecar
    runs one long-lived event loop and this key degenerates to just the
    URI, so we still get the caching win. In tests each async test
    typically gets its own loop, and this scheme gives each test its
    own client while lazily reaping the dead entries from previous
    loops (see the `stale` cleanup below).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Called outside an event loop (e.g. from a sync test). Fall back
        # to a per-URI key with sentinel loop id — safer than crashing.
        loop = None
    loop_id = id(loop) if loop is not None else 0
    key = (uri, loop_id)

    cached = _CLIENT_CACHE.get(key)
    if cached is not None:
        return cached

    # Reap entries for this URI whose loops are stale (closed or GC'd).
    # We can't cheaply check "was this loop closed?" from just the id, so
    # we just drop everything for this URI that doesn't match the current
    # loop_id — subsequent live loops will re-populate on demand.
    stale = [k for k in _CLIENT_CACHE if k[0] == uri and k[1] != loop_id]
    for k in stale:
        try:
            _CLIENT_CACHE[k].close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        del _CLIENT_CACHE[k]

    client = AsyncIOMotorClient(
        uri, serverSelectionTimeoutMS=server_selection_timeout_ms
    )
    _CLIENT_CACHE[key] = client
    return client


async def close_all_clients() -> None:
    """Close every cached motor client. Called from the FastAPI shutdown hook.

    motor's `.close()` is synchronous but this function is async for the
    caller's convenience — future work may want to await individual
    close operations if we adopt a client with an async shutdown path.
    """
    for client in list(_CLIENT_CACHE.values()):
        try:
            client.close()
        except Exception:  # noqa: BLE001 — best-effort during shutdown
            pass
    _CLIENT_CACHE.clear()
    _SCHEMA_CACHE.clear()


def invalidate_schema_cache(uri: str | None = None, db: str | None = None) -> None:
    """Drop cached schemas.

    * `uri=None, db=None` → clear everything (called on Refresh).
    * `uri=X, db=Y` → clear just that pair.
    """
    if uri is None and db is None:
        _SCHEMA_CACHE.clear()
        return
    key = (uri or "", db or "")
    _SCHEMA_CACHE.pop(key, None)

# System collections Mongo creates on its own. Never surface these to the UI.
# `system.*` collections are Mongo internals; `admin` and `local` are databases,
# not collections, but including the prefix here is defensive.
_MONGO_SYSTEM_PREFIXES: tuple[str, ...] = ("system.",)

# Per-query cap. Guards against a hung Mongo (or a genuinely huge query) from
# hanging the sidecar's event loop forever. 10 seconds is plenty for our
# aggregate/find operations at the sizes we target; if we ever legitimately
# need longer, we should push the specific caller to a background task
# rather than raising this ceiling globally.
_QUERY_MAX_TIME_MS: int = 10_000


class MongoConnector:
    """Async Mongo reader. Satisfies the `Connector` Protocol structurally."""

    def __init__(
        self,
        uri: str,
        db_name: str,
        *,
        sample_size: int = 20,
        server_selection_timeout_ms: int = 3_000,
    ) -> None:
        """
        Args:
            uri: Standard MongoDB connection URI.
            db_name: Target database name.
            sample_size: Max docs to sample per collection during schema
                introspection. Bounded so introspection stays cheap on large
                collections.
            server_selection_timeout_ms: Give up connecting after this many ms.
                Default 3s so the UI gets a fast error instead of hanging.
        """
        # Client is drawn from the module-level cache — see `_CLIENT_CACHE`.
        # We don't own it; `close()` is a no-op. Sidecar shutdown calls
        # `close_all_clients()` to release the pool cleanly.
        self._uri = uri
        self._db_name = db_name
        self._client: AsyncIOMotorClient = _get_or_create_client(
            uri, server_selection_timeout_ms=server_selection_timeout_ms
        )
        self._db = self._client[db_name]
        self._sample_size = sample_size
        self._closed = False

    async def introspect_schema(self) -> list[CollectionSchema]:
        """Return a schema-ish description of every user collection.

        Cached per (uri, db) for `_SCHEMA_TTL_SECONDS`. Both the /schema
        endpoint and the L1 scanner's auto-detection call this, and on
        a Refresh they can call it back-to-back — the cache saves the
        second call from re-sampling every collection.
        """
        key = (self._uri, self._db_name)
        now = time.monotonic()
        cached = _SCHEMA_CACHE.get(key)
        if cached is not None and (now - cached[0]) < _SCHEMA_TTL_SECONDS:
            return cached[1]
        result = await self._introspect_schema_uncached()
        _SCHEMA_CACHE[key] = (now, result)
        return result

    async def _introspect_schema_uncached(self) -> list[CollectionSchema]:
        """The actual introspection work — bypasses the cache."""
        names = await self._db.list_collection_names()
        results: list[CollectionSchema] = []
        for name in sorted(names):
            if any(name.startswith(prefix) for prefix in _MONGO_SYSTEM_PREFIXES):
                continue
            coll = self._db[name]
            # estimated_document_count is O(1) — uses collection metadata.
            # For non-mapped collections in the UI picker, that's plenty accurate.
            count = await coll.estimated_document_count(maxTimeMS=_QUERY_MAX_TIME_MS)
            fields: set[str] = set()
            cursor = (
                coll.find({}, projection=None)
                .limit(self._sample_size)
                .max_time_ms(_QUERY_MAX_TIME_MS)
            )
            async for doc in cursor:
                fields.update(doc.keys())
            results.append(
                CollectionSchema(
                    name=name,
                    document_count=count,
                    field_names=sorted(fields),
                )
            )
        return results

    async def stream_rows(self, collection: str) -> AsyncIterator[dict[str, Any]]:
        """Yield every document in `collection`, unmodified."""
        coll = self._db[collection]
        cursor = coll.find({}).max_time_ms(_QUERY_MAX_TIME_MS)
        async for doc in cursor:
            yield doc

    async def count_by_field(
        self,
        collection: str,
        group_field: str,
    ) -> dict[str, int]:
        """Return a `{group_value_str: count}` map for `collection`.

        Uses a single `$group` aggregation — Mongo can serve this from an
        index if one exists on `group_field`, and it doesn't fetch any
        document bodies, only the group key + count. That's what makes the
        "lazy files" behavior cheap: on a workflow with 10,000 files we
        touch metadata, not payload.

        Documents whose `group_field` is null are excluded (they don't have
        a parent to attribute to).
        """
        coll = self._db[collection]
        pipeline = [{"$group": {"_id": f"${group_field}", "count": {"$sum": 1}}}]
        result: dict[str, int] = {}
        async for doc in coll.aggregate(pipeline, maxTimeMS=_QUERY_MAX_TIME_MS):
            key = doc.get("_id")
            if key is None:
                continue
            result[str(key)] = int(doc.get("count", 0))
        return result

    async def find_by_field(
        self,
        collection: str,
        field: str,
        value: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every document in `collection` where `field == value`.

        Used by the subtree endpoint to fetch (say) all files with a
        specific `workflow_id`. Async iterator so we don't materialize the
        whole result set in memory — matters when a "small" subtree still
        contains thousands of rows.
        """
        coll = self._db[collection]
        cursor = coll.find({field: value}).max_time_ms(_QUERY_MAX_TIME_MS)
        async for doc in cursor:
            yield doc

    async def close(self) -> None:
        """No-op — the underlying motor client is process-cached.

        Kept on the class so `async with MongoConnector(...)` still works
        and the `Connector` Protocol contract is satisfied. The pool is
        released process-wide via `close_all_clients()` on FastAPI
        shutdown.
        """
        self._closed = True

    async def __aenter__(self) -> "MongoConnector":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        # Wrapped in the dunder so `async with MongoConnector(...) as c` works,
        # which is the pattern the endpoint layer uses to guarantee close-on-error.
        await self.close()
