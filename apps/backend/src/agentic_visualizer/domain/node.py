"""Node — the primary graph vertex.

Design decisions worth naming here so they don't drift:

* **`id` is a namespaced string, not an integer or UUID.** Format is
  `<source>:<subtype>:<local_id>` — e.g. `mongo:agents:400000000000000000000001`
  for a Mongo doc, later `code:src/tools/langchain_tools.py:14:web_search_tool`
  for a code function. Stable, deterministic, human-readable in logs, and gives
  the merge story a solid `(source, source_ref)` identity for free — two
  scanner runs against unchanged sources produce the same IDs, so the deltas
  are meaningful.

* **`attributes` is deliberately open.** The visualizer's detail panel renders
  whatever's in there, unflattened. If a scanner wants to surface a nested
  config blob, it just puts it in `attributes` — no schema migration required.
  The trade-off: we can't rely on typed access to attribute contents anywhere
  outside the scanner that produced them. That's a feature, not a bug: it
  keeps the graph engine framework-agnostic.

* **Provenance is required, not optional.** Every node must know which scanner
  produced it and where it came from. Without provenance, the diff-on-refresh
  story (see CLAUDE.md §6) can't work, and the detail panel can't answer
  "where did this come from?"
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# Shared model_config for every domain type.
#
# `alias_generator=to_camel` — Python fields stay snake_case (idiomatic on our
# side) but serialize as camelCase (idiomatic for TS/JSON consumers). Pydantic
# generates one alias per field automatically: `source_id` ↔ `sourceId`,
# `scanned_at` ↔ `scannedAt`, etc.
#
# `populate_by_name=True` — allows constructing the model with either name
# (snake_case field name OR camelCase alias). Without this, tests and code
# that build models with `Node(id=..., name=...)` would break the moment we
# added the alias generator, because Pydantic would only accept the alias.
#
# `frozen=True` — every domain value type is immutable. Enforced across all
# of them because mutation-in-place would silently break the "merge is
# deterministic" contract in CLAUDE.md §6.
_DOMAIN_CONFIG = ConfigDict(
    frozen=True,
    alias_generator=to_camel,
    populate_by_name=True,
)

# Every node type the graph can render. Extend here when a new scanner or
# framework adapter introduces a new concept. Kept as a Literal (not an Enum)
# because it round-trips to JSON as plain strings, matches TS discriminated
# unions cleanly, and doesn't force callers to import the enum symbol.
NodeType = Literal[
    "workflow",
    "agent",
    "model",
    "tool",
    "prompt",
    "user",
    "file",
    # Reserved for later stages — declared now so downstream code (UI palette,
    # legend, filters) can be built once and never revisited when L2/L3/L4 land.
    "function",
    "endpoint",
]


class Provenance(BaseModel):
    """Where a node/edge came from. Drives the detail panel and refresh diffs."""

    model_config = _DOMAIN_CONFIG

    source: str
    """Which scanner produced this element. e.g. 'l1_entity', 'l2_tool_resolver'."""

    source_ref: str
    """A stable pointer back to the original source.

    Format depends on the scanner:
      * l1_entity        → f'{db}.{collection}:{oid}'
      * l2_tool_resolver → f'{filepath}:{lineno}:{qualname}'
      * l3_call_graph    → f'{filepath}:{lineno}:{qualname}'
      * l4_endpoint      → f'{filepath}:{lineno}:{method} {path}'
    """

    scanned_at: datetime
    """When the scanner ran. Enables 'stale since X' detection on refresh."""


class Node(BaseModel):
    """A vertex in the graph.

    Two scanner runs against unchanged sources MUST produce nodes with
    identical `id`s. That invariant is what lets the merge step be
    non-destructive (see CLAUDE.md §6 — "reconcile by stable identity").
    """

    model_config = _DOMAIN_CONFIG

    id: str = Field(min_length=1)
    type: NodeType
    name: str = Field(min_length=1)
    provenance: Provenance
    attributes: dict[str, Any] = Field(default_factory=dict)
