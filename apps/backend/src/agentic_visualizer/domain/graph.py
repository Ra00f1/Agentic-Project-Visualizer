"""Graph, ScanResult, ScanError.

`ScanResult` is what every scanner returns. `Graph` is what you get when you
merge scan results together for the UI to render. Kept separate deliberately:
scanners produce, the merger reconciles, the UI consumes. Mixing the layers
would push merge logic into scanners.

For v1 we only run one scanner (L1), so "merging" is trivial. But the shape
of ScanResult already reflects the multi-scanner world — errors are per-scan,
not global — so L2/L3/L4 slot in without a refactor.
"""

from __future__ import annotations

from typing import Iterable

from pydantic import BaseModel, Field

from .edge import Edge
from .node import Node, _DOMAIN_CONFIG


class ScanError(BaseModel):
    """A non-fatal problem the scanner encountered.

    Examples: a Mongo doc missing a required field, an L2 tool row pointing
    at a code function that doesn't exist, an L3 `importlib.import_module`
    call the AST scanner can't resolve. Errors are DATA, not exceptions —
    they show up in the UI as warnings on the offending node or as a global
    scan-warnings pane. The scan itself continues.

    A scanner should raise only on true infrastructure failure (DB
    unreachable, filesystem unreadable) — those are exceptional and bubble
    up to the endpoint layer for a 5xx.
    """

    model_config = _DOMAIN_CONFIG

    scanner: str
    """Which scanner produced the error. Matches `Provenance.source`."""

    source_ref: str
    """Best-effort pointer to the source of the problem."""

    message: str
    """Human-readable description. Shown verbatim in the UI."""


class ScanResult(BaseModel):
    """What a `Scanner.scan(...)` call returns.

    Node/edge lists are inputs to the merger, not the final graph — a scanner
    can emit duplicate nodes/edges (same id) and the merger deduplicates.
    That deliberately keeps scanners branch-free.
    """

    model_config = _DOMAIN_CONFIG

    scanner: str
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    errors: list[ScanError] = Field(default_factory=list)


class Graph(BaseModel):
    """The merged output of one or more `ScanResult`s.

    Shape is intentionally boring: two flat lists, ready to serialize to the
    frontend and hand straight to React Flow. Any richer structure (adjacency
    maps, indexes for search) belongs in the UI layer or in a specialized
    query service, not here — this is the persisted domain, and adding
    structure means adding invalidation.
    """

    model_config = _DOMAIN_CONFIG

    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    errors: list[ScanError] = Field(default_factory=list)

    @classmethod
    def from_results(cls, results: Iterable[ScanResult]) -> "Graph":
        """Merge a sequence of scan results into one graph.

        Deduplication rules:
          * Nodes: `id` is the key. First-writer-wins on collisions — later
            scanners cannot mutate an earlier scanner's node. This preserves
            provenance and matches the "later stages consume upstream output
            but never mutate it" contract in CLAUDE.md §6.
          * Edges: `(source_id, target_id, kind)` is the key. Same
            first-writer-wins rule.

        Edges whose endpoints don't exist as nodes in the merged set are kept
        (they may resolve when a later scanner runs) but they'd surface as
        broken lines in the UI. If we want to hide them, that's a UI concern.
        """
        seen_nodes: dict[str, Node] = {}
        seen_edges: dict[tuple[str, str, str], Edge] = {}
        errors: list[ScanError] = []
        for result in results:
            for node in result.nodes:
                seen_nodes.setdefault(node.id, node)
            for edge in result.edges:
                key = (edge.source_id, edge.target_id, edge.kind)
                seen_edges.setdefault(key, edge)
            errors.extend(result.errors)
        return cls(
            nodes=list(seen_nodes.values()),
            edges=list(seen_edges.values()),
            errors=errors,
        )
