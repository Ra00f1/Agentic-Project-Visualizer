"""Integration tests for L2ToolCodeScanner.

Runs L1 then L2 against the fixture MongoDB + the fixture codebase, then
asserts on the merged L2 delta (nodes + edges + errors). Every assertion
here is a specific promise the L2 scanner makes to the frontend — a
regression should fail a named test, not a vague count assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from agentic_visualizer.codebase import LocalCodebaseAdapter
from agentic_visualizer.connectors import MongoConnector
from agentic_visualizer.domain import Graph
from agentic_visualizer.scanners import L1EntityScanner, L2ToolCodeScanner


# Same path derivation as the codebase-adapter test — the fixture project
# sits at <repo>/mock-agent-project.
FIXTURE_ROOT = Path(__file__).parents[3] / "mock-agent-project"


# Fixture counts. Update alongside dump.json.
#
#   12 tools total in the DB.
#   10 resolve to a function node + implements edge:
#      -  9 via the direct dotted-path lookup.
#      -  1 (`moved_tool`) via the name-based fallback — its Mongo
#         `import_path` is stale but the function still exists under
#         `src/skills/formatting.py`.
#    2 stay unresolved (neither direct nor fallback finds them):
#      - missing_impl_tool  (module exists, function doesn't AND no
#                            function anywhere else matches the name)
#      - phantom_module_tool (module doesn't exist AND no matching name)
EXPECTED_FUNCTION_NODES = 10
EXPECTED_IMPLEMENTS_EDGES = 10
EXPECTED_L2_ERRORS = 2


@pytest_asyncio.fixture
async def l1_result(require_mongo, scan_context_factory):
    """L1 scan of the fixture DB — needed as `upstream` for every L2 test."""
    uri, db = require_mongo
    async with MongoConnector(uri=uri, db_name=db) as connector:
        result = await L1EntityScanner(db_name=db).scan(scan_context_factory(connector))
    return result


@pytest.fixture
def adapter() -> LocalCodebaseAdapter:
    assert FIXTURE_ROOT.exists(), f"fixture missing: {FIXTURE_ROOT}"
    return LocalCodebaseAdapter(root=FIXTURE_ROOT)


@pytest.mark.asyncio
async def test_l2_emits_expected_function_and_edge_counts(
    l1_result, adapter, scan_context_factory
) -> None:
    """L2's node/edge output is stable at fixture scale.

    Uses a mock connector (None) since L2 doesn't touch the DB — L1's
    result is the only input it cares about. `scan_context_factory`
    accepts anything for the connector slot.
    """
    result = await L2ToolCodeScanner(codebase=adapter, upstream=l1_result).scan(
        scan_context_factory(None)
    )
    function_nodes = [n for n in result.nodes if n.type == "function"]
    implements_edges = [e for e in result.edges if e.kind == "implements"]
    assert len(function_nodes) == EXPECTED_FUNCTION_NODES, (
        f"expected {EXPECTED_FUNCTION_NODES} function nodes, got "
        f"{[n.name for n in function_nodes]}"
    )
    assert len(implements_edges) == EXPECTED_IMPLEMENTS_EDGES


@pytest.mark.asyncio
async def test_l2_emits_error_per_unresolved_tool(
    l1_result, adapter, scan_context_factory
) -> None:
    """Both distractors surface as ScanErrors — one per tool, keyed by the
    tool's own node id so the frontend can attach a per-node warning."""
    result = await L2ToolCodeScanner(codebase=adapter, upstream=l1_result).scan(
        scan_context_factory(None)
    )
    assert len(result.errors) == EXPECTED_L2_ERRORS, result.errors

    # Bucket errors by which tool they name.
    by_tool_name: dict[str, str] = {}
    for e in result.errors:
        tool_node = next((n for n in l1_result.nodes if n.id == e.source_ref), None)
        assert tool_node is not None, (
            f"L2 emitted an error whose source_ref isn't a tool node id: {e}"
        )
        by_tool_name[tool_node.name] = e.message

    assert "missing_impl_tool" in by_tool_name
    assert "phantom_module_tool" in by_tool_name

    # Distinct message shapes — the frontend uses these verbatim in the
    # warning tooltip, and operators should be able to tell the two failure
    # modes apart at a glance.
    assert "module was found" in by_tool_name["missing_impl_tool"], by_tool_name
    assert "module not found" in by_tool_name["phantom_module_tool"], by_tool_name


@pytest.mark.asyncio
async def test_l2_llamaindex_tools_resolve_to_underlying_private_functions(
    l1_result, adapter, scan_context_factory
) -> None:
    """The three LlamaIndex tools point at module-level `..._tool` variables;
    L2 must unwrap `FunctionTool.from_defaults(fn=...)` and land the
    implements edge on the underlying `_..._tool` function.

    This is the whole reason `SymbolLocation.kind` exists — verify all
    three carry `resolution_kind="llamaindex_wrapped"` on their function
    nodes, and the qualname is the underlying (underscored) name.
    """
    result = await L2ToolCodeScanner(codebase=adapter, upstream=l1_result).scan(
        scan_context_factory(None)
    )
    function_names = {n.name for n in result.nodes if n.type == "function"}
    for expected in {"_document_query", "_index_ingest", "_citation_lookup"}:
        assert expected in function_names, (
            f"LlamaIndex unwrap missed {expected}; "
            f"got {sorted(function_names)}"
        )

    wrapped = [
        n for n in result.nodes
        if n.type == "function"
        and n.attributes.get("resolution_kind") == "llamaindex_wrapped"
    ]
    assert len(wrapped) == 3


@pytest.mark.asyncio
async def test_l2_never_emits_the_undecorated_helper_as_a_tool_function(
    l1_result, adapter, scan_context_factory
) -> None:
    """`utils.decorators.looks_like_a_tool_but_isnt` has a tool-shaped
    signature but no Mongo record. The tool-driven L2 must not emit a
    function node for it — that would be a false positive that hides the
    real intent of L2 (surface only what the DB claims is a tool).
    """
    result = await L2ToolCodeScanner(codebase=adapter, upstream=l1_result).scan(
        scan_context_factory(None)
    )
    names = {n.name for n in result.nodes if n.type == "function"}
    assert "looks_like_a_tool_but_isnt" not in names, (
        "L2 leaked the undecorated-non-tool helper as a function node"
    )


@pytest.mark.asyncio
async def test_merged_graph_has_every_implements_target_present(
    l1_result, adapter, scan_context_factory
) -> None:
    """Every `implements` edge in the merged graph must point at a real
    function node — dangling edges break the graph's integrity contract
    and would surface as ghost edges in the UI. This test guards against
    an id-format drift between the scanner's function-id builder and the
    edge target computation."""
    l2_result = await L2ToolCodeScanner(codebase=adapter, upstream=l1_result).scan(
        scan_context_factory(None)
    )
    graph = Graph.from_results([l1_result, l2_result])
    node_ids = {n.id for n in graph.nodes}
    for edge in graph.edges:
        if edge.kind != "implements":
            continue
        assert edge.source_id in node_ids, f"dangling implements source: {edge}"
        assert edge.target_id in node_ids, f"dangling implements target: {edge}"


@pytest.mark.asyncio
async def test_l2_is_deterministic(
    l1_result, adapter, scan_context_factory
) -> None:
    """Running L2 twice against the same inputs yields identical node ids
    and edge triples. That determinism is what makes the refresh + merge
    story sane — if L2 produced different IDs run-to-run, every refresh
    would show every function as "new."
    """
    r1 = await L2ToolCodeScanner(codebase=adapter, upstream=l1_result).scan(
        scan_context_factory(None)
    )
    r2 = await L2ToolCodeScanner(codebase=adapter, upstream=l1_result).scan(
        scan_context_factory(None)
    )
    assert {n.id for n in r1.nodes} == {n.id for n in r2.nodes}
    assert {(e.source_id, e.target_id, e.kind) for e in r1.edges} == {
        (e.source_id, e.target_id, e.kind) for e in r2.edges
    }



@pytest.mark.asyncio
async def test_l2_uses_name_fallback_when_direct_import_path_is_stale(
    l1_result, adapter, scan_context_factory
) -> None:
    """`moved_tool` in the fixture has a stale `import_path` pointing at
    `src.tools.custom_tools.format_output_tool` (which doesn't exist there),
    but the underlying function LIVES at `src/skills/formatting.py`. L2
    should find it via the name-based fallback and emit a function node
    for it — no ScanError.

    This is what proves L2 doesn't rely on tools living under a folder
    named `tools/` or a file named `*_tools.py`.
    """
    result = await L2ToolCodeScanner(codebase=adapter, upstream=l1_result).scan(
        scan_context_factory(None)
    )
    names = {n.name for n in result.nodes if n.type == "function"}
    assert "format_output_tool" in names, sorted(names)
    # Its function node's file must be the NEW location, not the stale one.
    fmt_node = next(
        n for n in result.nodes
        if n.type == "function" and n.name == "format_output_tool"
    )
    assert fmt_node.attributes["file_path"] == "src/skills/formatting.py"
    # And moved_tool must NOT appear in the error list.
    for e in result.errors:
        assert "moved_tool" not in e.message, (
            f"moved_tool should have resolved via fallback, but got error: {e}"
        )
