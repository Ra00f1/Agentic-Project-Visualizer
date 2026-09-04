"""LlamaIndex-flavored tool definitions.

Real LlamaIndex constructs tools with `FunctionTool.from_defaults(fn=...)`. The
tool NODE (in DB) references the wrapped function by name, not the variable
that holds the FunctionTool. So the scanner needs to:

  1. Recognize the `FunctionTool.from_defaults(fn=<name>)` call pattern.
  2. Follow `fn=` to the underlying function definition in this module.

We shim `FunctionTool` locally so this file parses without a runtime dep.
"""

from __future__ import annotations

from typing import Any


class FunctionTool:
    """Local shim mirroring `llama_index.core.tools.FunctionTool` shape."""

    def __init__(self, fn: Any, name: str | None = None, description: str | None = None):
        self.fn = fn
        self.name = name or fn.__name__
        self.description = description or (fn.__doc__ or "").strip()

    @classmethod
    def from_defaults(
        cls,
        fn: Any,
        name: str | None = None,
        description: str | None = None,
    ) -> "FunctionTool":
        return cls(fn=fn, name=name, description=description)


# -- wrapped functions -------------------------------------------------------

def _document_query(index_name: str, query: str, top_k: int = 3) -> list[dict]:
    """Query a LlamaIndex vector index for the top-k matching documents."""
    return []  # mock


def _index_ingest(source_uri: str, index_name: str) -> dict:
    """Ingest a document from `source_uri` into the named index."""
    return {"index": index_name, "ingested": 0, "source": source_uri}  # mock


def _citation_lookup(document_id: str) -> dict:
    """Return the citation metadata for a stored document."""
    return {"document_id": document_id, "citation": None}  # mock


# -- exposed FunctionTool instances -----------------------------------------
# The Mongo `tools` records reference these by the wrapped-function name
# (e.g., "_document_query"), not by the variable name. Adapter must resolve
# both directions.

document_query_tool = FunctionTool.from_defaults(
    fn=_document_query,
    name="document_query",
    description="Retrieve top-k matching documents from a vector index.",
)

index_ingest_tool = FunctionTool.from_defaults(
    fn=_index_ingest,
    name="index_ingest",
    description="Add a new document to a vector index.",
)

citation_lookup_tool = FunctionTool.from_defaults(
    fn=_citation_lookup,
    name="citation_lookup",
)
