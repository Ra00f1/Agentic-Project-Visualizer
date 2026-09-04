"""Domain model unit tests.

Pure Pydantic — no DB, no I/O, no async. Guards the invariants documented in
the domain module docstrings.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agentic_visualizer.domain import (
    Edge,
    Graph,
    Node,
    Provenance,
    ScanError,
    ScanResult,
)


def _prov() -> Provenance:
    return Provenance(
        source="l1_entity",
        source_ref="mock.users:1",
        scanned_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_node_requires_nonempty_id_and_name() -> None:
    with pytest.raises(ValidationError):
        Node(id="", type="user", name="alice", provenance=_prov())
    with pytest.raises(ValidationError):
        Node(id="mongo:mock.users:1", type="user", name="", provenance=_prov())


def test_node_type_literal_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        Node(
            id="mongo:mock.users:1",
            type="ghost",  # type: ignore[arg-type]
            name="alice",
            provenance=_prov(),
        )


def test_node_is_frozen() -> None:
    node = Node(id="mongo:mock.users:1", type="user", name="alice", provenance=_prov())
    with pytest.raises(ValidationError):
        node.name = "bob"  # type: ignore[misc]


def test_edge_requires_endpoints() -> None:
    with pytest.raises(ValidationError):
        Edge(source_id="", target_id="b", kind="uses", provenance=_prov())
    with pytest.raises(ValidationError):
        Edge(source_id="a", target_id="", kind="uses", provenance=_prov())


def test_graph_merger_dedupes_nodes_and_edges() -> None:
    node = Node(id="n1", type="user", name="alice", provenance=_prov())
    edge = Edge(source_id="n1", target_id="n2", kind="uses", provenance=_prov())
    r1 = ScanResult(scanner="s1", nodes=[node], edges=[edge])
    r2 = ScanResult(scanner="s2", nodes=[node], edges=[edge])
    merged = Graph.from_results([r1, r2])
    assert len(merged.nodes) == 1
    assert len(merged.edges) == 1


def test_graph_merger_first_writer_wins() -> None:
    node_a = Node(id="n1", type="user", name="alice", provenance=_prov())
    node_b = Node(id="n1", type="user", name="alice-renamed", provenance=_prov())
    r1 = ScanResult(scanner="s1", nodes=[node_a])
    r2 = ScanResult(scanner="s2", nodes=[node_b])
    merged = Graph.from_results([r1, r2])
    assert len(merged.nodes) == 1
    assert merged.nodes[0].name == "alice"  # r1 wins


def test_graph_merger_collects_all_errors() -> None:
    e1 = ScanError(scanner="s1", source_ref="x", message="oops")
    e2 = ScanError(scanner="s2", source_ref="y", message="oops again")
    merged = Graph.from_results(
        [
            ScanResult(scanner="s1", errors=[e1]),
            ScanResult(scanner="s2", errors=[e2]),
        ]
    )
    assert [e.message for e in merged.errors] == ["oops", "oops again"]
