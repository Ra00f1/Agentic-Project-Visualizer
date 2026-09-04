"""Connector Protocol — the seam scanners consume.

Chose `typing.Protocol` over an `abc.ABC` for two reasons:

1. **No forced inheritance.** A third-party adapter can be a Connector by
   virtue of shape alone; it doesn't need to import our base class. That
   matters if we ever open the connector interface to plugins.
2. **Structural typing plays nicer with mypy.** A concrete Mongo/Postgres
   connector satisfies the Protocol as long as the method signatures line up.
   No `@abstractmethod` decorators to forget, no runtime metaclass magic.

The trade-off: Protocol members are not enforced at runtime. Missing methods
become AttributeError at the call site, not at import time. We mitigate that
by running mypy in CI (see pyproject.toml) — a Connector implementation with
a bad signature fails typecheck.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CollectionSchema(BaseModel):
    """A shallow description of a table/collection.

    Populated by `introspect_schema`. For Mongo this is inferred from a small
    sample of documents (Mongo is schemaless — there is no authoritative
    schema to read). For SQL databases it will come straight from
    INFORMATION_SCHEMA. Kept intentionally simple: enough for the UI to show
    a "which tables map to which node types?" picker, no more.

    Same alias_generator config as the domain models — camelCase on the wire
    (`documentCount`, `fieldNames`), snake_case in Python.
    """

    model_config = ConfigDict(
        frozen=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )

    name: str
    """Collection/table name."""

    document_count: int | None = None
    """Approximate document count. `None` if the connector can't cheaply obtain it."""

    field_names: list[str] = Field(default_factory=list)
    """Union of top-level field names observed in the sample."""


@runtime_checkable
class Connector(Protocol):
    """Read-only interface over a user's data source.

    Scanners depend on this Protocol; concrete implementations live alongside
    (mongo.py, postgres.py, ...). Every method is async — see CLAUDE.md §5
    ("async by default on IO paths").

    Contract notes:

    * `introspect_schema` returns EVERY collection/table the connector can
      see, not just ones the user has mapped. The UI needs the full list to
      let the user do the mapping.
    * `stream_rows` yields rows one at a time, not batched. Batching is a
      connector-internal concern (motor cursor, SQLAlchemy fetchmany) — the
      scanner should not have to think about it.
    * `close` must be idempotent. Calling it twice must not raise.
    * A Connector instance is single-use for the duration of one scan.
      Sharing across scans is fine but sharing across concurrent scans is
      not — connection pooling is the caller's job.
    """

    async def introspect_schema(self) -> list[CollectionSchema]:
        """List every collection/table the connector can see."""
        ...

    async def stream_rows(self, collection: str) -> AsyncIterator[dict[str, Any]]:
        """Yield every row/document in `collection` as a plain dict.

        Row shape is connector-native: for Mongo, `_id` is a `bson.ObjectId`;
        for SQL, keys are column names and values are Python-typed. Callers
        (scanners) do the type normalization they need.
        """
        ...

    async def close(self) -> None:
        """Release underlying resources. Idempotent."""
        ...
