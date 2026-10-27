"""L1 entity scanner integration test.

Runs the scanner against the seeded `mock_agent` DB and asserts the shape
of the resulting graph matches what the fixture is meant to produce.

If someone changes the fixture in `mock-agent-project/mongo/dump.json`, these
counts change and this file must be updated deliberately. That's the point —
these are the guard-rails for the L1 contract.
"""

from __future__ import annotations

import pytest

from agentic_visualizer.connectors import MongoConnector
from agentic_visualizer.domain import Graph
from agentic_visualizer.scanners import L1EntityScanner


# Node counts by type — must match the fixture. users + prompts are
# excluded from the current mapping (see l1_entity.py). files are lazy —
# the /graph endpoint no longer emits file nodes, they arrive via
# /graph/subtree on demand.
#
# Reconciled 2026-10-06 (Task 10) against a fresh `python mongo/seed.py --drop`.
# The fixture (`mock-agent-project/mongo/dump.json` + seed.py's two
# programmatically-generated structures — 1200 bulk files for
# archive_curator, and the 61-agent large_scale_moderation workflow) grew
# substantially since these counts were last hand-derived (Tasks 1-4);
# recomputed here directly from the live DB (`db[coll].count_documents({})`)
# plus the scanner's own output, cross-checked against each other rather
# than trusting either alone. Update alongside dump.json/seed.py.
#
# Along the way this surfaced a real seed.py bug (also fixed in Task 10):
# the generated `large_scale_moderation` workflow's hardcoded `_id`
# collided with dump.json's own `customer_support_pipeline` workflow, so
# every `--drop` reseed silently overwrote one hand-curated workflow with
# the generated one (seed.py's own "22 upserted" log line never matched the
# 21 documents actually left in the collection). Fixed in seed.py
# (MOD_WORKFLOW_OID); workflow count below is the now-correct 22.
EXPECTED_NODES_BY_TYPE = {
    "model": 8,
    "tool": 78,
    "agent": 133,
    "workflow": 22,
    # "file": 0 (lazy — fetched via /graph/subtree)
}

# Edge counts by kind. Files are lazy, so no contains edges appear here.
# Verified two independent ways: (1) the scanner's own output, (2) a
# from-scratch recount straight off the raw Mongo docs (summing
# model_id/tool_ids/sub_agent_ids/entrypoint_agent_id/agent_ids/model_ids
# across every agent/workflow doc) — both agree exactly.
#
# Two refs in the fixture are DELIBERATELY broken (mock-agent-project's
# README, "Broken references" section: "L1 should surface as unresolved") —
# an agent's model_id and another agent's tool_ids entry point at ids that
# don't exist in their target collections. Task 10 fixed a real scanner bug
# here: these used to silently become dangling edges (no node at the other
# end); they now get excluded from `edges` and surfaced as ScanErrors
# instead, matching the fixture's documented intent —
# `test_l1_scan_edges_point_to_real_nodes` below is this behavior's
# regression guard (it was the test actually failing against the broken-ref
# fixture before this fix). EXPECTED_EDGES_BY_KIND below already excludes
# both broken refs.
EXPECTED_EDGES_BY_KIND = {
    "uses": 511,
    "delegates_to": 100,
    # "contains": 0 (lazy)
}
EXPECTED_BROKEN_REF_ERRORS = 2

# Post-merger count. `Graph.from_results` dedups by (source, target, kind).
# At this fixture's current scale, 20 workflows list their own
# `entrypoint_agent_id` a second time inside `agent_ids` — each produces two
# `workflow → agent (uses)` Edge objects for the identical (source, target,
# kind) triple (one from each RefSpec), which the merger collapses to one.
# That's true even merging a SINGLE scan result with itself (this test's own
# case) — the duplication already exists within one scan's raw edge list,
# not something that only appears across two different scans.
EXPECTED_MERGED_EDGE_COUNT = sum(EXPECTED_EDGES_BY_KIND.values()) - 20


@pytest.mark.asyncio
async def test_l1_scan_produces_expected_node_counts(
    require_mongo, scan_context_factory
) -> None:
    uri, db = require_mongo
    async with MongoConnector(uri=uri, db_name=db) as connector:
        result = await L1EntityScanner(db_name=db).scan(scan_context_factory(connector))

    by_type: dict[str, int] = {}
    for node in result.nodes:
        by_type[node.type] = by_type.get(node.type, 0) + 1
    assert by_type == EXPECTED_NODES_BY_TYPE, by_type


@pytest.mark.asyncio
async def test_l1_scan_produces_expected_edge_counts(
    require_mongo, scan_context_factory
) -> None:
    uri, db = require_mongo
    async with MongoConnector(uri=uri, db_name=db) as connector:
        result = await L1EntityScanner(db_name=db).scan(scan_context_factory(connector))

    by_kind: dict[str, int] = {}
    for edge in result.edges:
        by_kind[edge.kind] = by_kind.get(edge.kind, 0) + 1
    assert by_kind == EXPECTED_EDGES_BY_KIND, by_kind


@pytest.mark.asyncio
async def test_l1_scan_edges_point_to_real_nodes(
    require_mongo, scan_context_factory
) -> None:
    """No dangling edges — every source/target id must exist as a node.

    The fixture has two DELIBERATELY broken refs (see EXPECTED_EDGES_BY_KIND's
    comment above) — this also asserts they surface as ScanErrors instead of
    silently becoming the dangling edges this test would otherwise catch.
    """
    uri, db = require_mongo
    async with MongoConnector(uri=uri, db_name=db) as connector:
        result = await L1EntityScanner(db_name=db).scan(scan_context_factory(connector))
    node_ids = {n.id for n in result.nodes}
    dangling = [
        (e.source_id, e.target_id, e.kind)
        for e in result.edges
        if e.source_id not in node_ids or e.target_id not in node_ids
    ]
    assert not dangling, dangling

    broken_ref_errors = [e for e in result.errors if "doesn't exist" in e.message]
    assert len(broken_ref_errors) == EXPECTED_BROKEN_REF_ERRORS, result.errors


@pytest.mark.asyncio
async def test_l1_scan_provenance_is_stable(
    require_mongo, scan_context_factory
) -> None:
    """Two scans with the same clock produce identical node ids and provenance."""
    uri, db = require_mongo
    async with MongoConnector(uri=uri, db_name=db) as c1:
        r1 = await L1EntityScanner(db_name=db).scan(scan_context_factory(c1))
    async with MongoConnector(uri=uri, db_name=db) as c2:
        r2 = await L1EntityScanner(db_name=db).scan(scan_context_factory(c2))

    ids1 = sorted(n.id for n in r1.nodes)
    ids2 = sorted(n.id for n in r2.nodes)
    assert ids1 == ids2


@pytest.mark.asyncio
async def test_l1_merged_graph_deduplicates(require_mongo, scan_context_factory) -> None:
    """Merging the same result twice must not double-count."""
    uri, db = require_mongo
    async with MongoConnector(uri=uri, db_name=db) as connector:
        result = await L1EntityScanner(db_name=db).scan(scan_context_factory(connector))
    graph = Graph.from_results([result, result])
    assert len(graph.nodes) == sum(EXPECTED_NODES_BY_TYPE.values())
    assert len(graph.edges) == EXPECTED_MERGED_EDGE_COUNT
