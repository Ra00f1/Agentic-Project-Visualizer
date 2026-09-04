"""Integration tests for L3CallGraphScanner.

Runs L3 against the fixture codebase and asserts on the shape of the
call-graph delta. Every assertion here is a specific promise the scanner
makes to the frontend — a regression should fail a named test, not a
vague count assertion.

Not testing every fine-grained resolution shape here (those live in
test_local_codebase_adapter.py for the adapter itself). This file is
about the end-to-end call graph as it appears in the visualizer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from agentic_visualizer.codebase import LocalCodebaseAdapter
from agentic_visualizer.scanners import L3CallGraphScanner


FIXTURE_ROOT = Path(__file__).parents[3] / "mock-agent-project"


@pytest.fixture
def adapter() -> LocalCodebaseAdapter:
    assert FIXTURE_ROOT.exists(), f"fixture missing: {FIXTURE_ROOT}"
    return LocalCodebaseAdapter(root=FIXTURE_ROOT)


@pytest_asyncio.fixture
async def l3_result(adapter, scan_context_factory):
    """Run L3 once per test — pytest-asyncio handles the coroutine fixture."""
    scanner = L3CallGraphScanner(codebase=adapter)
    return await scanner.scan(scan_context_factory(None))


# ---------------------------------------------------------------------------
# Basic emission — the scanner produces function nodes and calls edges at all.


@pytest.mark.asyncio
async def test_l3_emits_calls_edges(l3_result) -> None:
    """The fixture has plenty of intra-project calls; L3 must find them."""
    call_edges = [e for e in l3_result.edges if e.kind == "calls"]
    # Not asserting an exact count — the fixture grows over time and
    # brittle counts encourage skipping tests. Assert on the shape.
    assert len(call_edges) > 0, "expected at least some resolved calls"


@pytest.mark.asyncio
async def test_l3_never_emits_stdlib_targets(l3_result) -> None:
    """Every `calls` edge target must live inside the codebase (id starts
    with `code:` and the file lives under the fixture root's convention).
    A target pointing at asyncio.TaskGroup or dict.get is a bug."""
    for e in l3_result.edges:
        assert e.kind == "calls"
        assert e.target_id.startswith("code:"), e
        # file path segment (between the two colons) must not start with
        # dotted stdlib module names.
        _, rest = e.target_id.split(":", 1)
        file_path = rest.split(":", 1)[0]
        assert not file_path.startswith("asyncio"), e
        assert not file_path.startswith("typing"), e


# ---------------------------------------------------------------------------
# The 3-hop canonical chain — the fixture's raison d'être for L3.
#
#   api/routes.py:list_documents
#      → services/search_service.py:search
#            → repositories/document_repo.py:DocumentRepository.fetch_all
#
# All three edges must exist end-to-end. This is the test that fails
# loudest if module-alias resolution or class-instance singletons break.


def _find_edge(result, source_ends_with: str, target_ends_with: str):
    for e in result.edges:
        if e.kind != "calls":
            continue
        if e.source_id.endswith(source_ends_with) and e.target_id.endswith(target_ends_with):
            return e
    return None


@pytest.mark.asyncio
async def test_l3_three_hop_chain_list_documents_to_search(l3_result) -> None:
    """`list_documents` calls `search_service.search(...)`. The scanner
    must follow the module alias (`from ..services import search_service`)
    and land on the top-level `search` in search_service.py."""
    edge = _find_edge(l3_result, "list_documents", "search")
    assert edge is not None, (
        "expected calls edge list_documents -> search. "
        f"got {[(e.source_id, e.target_id) for e in l3_result.edges if e.kind == 'calls' and 'list_documents' in e.source_id]}"
    )


@pytest.mark.asyncio
async def test_l3_three_hop_chain_search_to_fetch_all(l3_result) -> None:
    """`search` calls `default_repo.fetch_all(...)`. `default_repo` is a
    module-level `DocumentRepository()` singleton — the scanner must
    trace it to the class + method. This is the trickiest resolution
    shape in v1; the class-instance tracking pass earns its keep here."""
    edge = _find_edge(l3_result, ":search", "DocumentRepository.fetch_all")
    assert edge is not None, (
        "expected calls edge search -> DocumentRepository.fetch_all. "
        f"got {[(e.source_id, e.target_id) for e in l3_result.edges if e.kind == 'calls']}"
    )


# ---------------------------------------------------------------------------
# Fan-in — `normalize_query` is called from both langchain_tools and
# custom_tools. The two callers must resolve to the SAME target node id
# so the frontend renders one function node with two incoming edges.


@pytest.mark.asyncio
async def test_l3_normalize_query_fan_in(l3_result) -> None:
    """Fan-in survives: both callers land on the same target id."""
    incoming = [
        e for e in l3_result.edges
        if e.kind == "calls" and e.target_id.endswith(":normalize_query")
    ]
    assert len(incoming) == 2, (
        f"expected 2 calls to normalize_query (langchain + custom), got "
        f"{len(incoming)}: {[e.source_id for e in incoming]}"
    )
    # All two must point at the SAME target node id — same file, line, qualname.
    targets = {e.target_id for e in incoming}
    assert len(targets) == 1, (
        f"fan-in broken: normalize_query was resolved to {len(targets)} "
        f"different target ids: {targets}"
    )


@pytest.mark.asyncio
async def test_l3_build_result_envelope_fan_in(l3_result) -> None:
    """`build_result_envelope` is the other shared helper — called from
    every custom tool. Similar fan-in verification (relaxed count since
    it's called from more than two sites in the fixture)."""
    incoming = [
        e for e in l3_result.edges
        if e.kind == "calls" and e.target_id.endswith(":build_result_envelope")
    ]
    assert len(incoming) >= 2, f"expected >= 2 calls, got {len(incoming)}"
    targets = {e.target_id for e in incoming}
    assert len(targets) == 1, f"fan-in broken: {targets}"


# ---------------------------------------------------------------------------
# Async function bodies — TaskGroup wraps _complete_one, but the call
# still attributes to `complete_many` (not lost inside the context mgr).


@pytest.mark.asyncio
async def test_l3_async_function_body_calls_are_captured(l3_result) -> None:
    """`complete_many` calls `_complete_one` (inside a TaskGroup context).
    The scanner must NOT lose the call inside the `async with` block."""
    edge = _find_edge(l3_result, ":complete_many", ":_complete_one")
    assert edge is not None, (
        f"expected complete_many -> _complete_one. "
        f"available: {[(e.source_id, e.target_id) for e in l3_result.edges if e.kind == 'calls' and 'complete' in e.source_id]}"
    )


@pytest.mark.asyncio
async def test_l3_async_function_direct_call(l3_result) -> None:
    """Simpler async case: `complete` calls `_complete_one` directly."""
    edge = _find_edge(l3_result, ":complete", ":_complete_one")
    assert edge is not None


# ---------------------------------------------------------------------------
# Aliased imports — `from ..tools import custom_tools as ct` then
# `ct.file_write_tool(...)`. The alias must resolve.


@pytest.mark.asyncio
async def test_l3_aliased_module_import(l3_result) -> None:
    """coordinator_agent.py imports `custom_tools as ct` and its
    `save_result` method calls `ct.file_write_tool(...)`. The scanner
    should resolve to custom_tools.py:file_write_tool.

    Note: `save_result` is a method — v1 doesn't extract methods as
    top-level defs. So this edge WON'T appear in v1. Documenting that
    expectation with an xfail so we notice when method support lands."""
    edge = _find_edge(l3_result, "save_result", "file_write_tool")
    # v1: we don't walk method bodies. Expected: edge is None.
    assert edge is None, (
        "Unexpectedly resolved a method-body call — if v2 added class "
        "method support, flip this assertion and delete the xfail comment."
    )


# ---------------------------------------------------------------------------
# Determinism — running twice produces identical output.


@pytest.mark.asyncio
async def test_l3_is_deterministic(adapter, scan_context_factory) -> None:
    r1 = await L3CallGraphScanner(codebase=adapter).scan(scan_context_factory(None))
    # Use a fresh adapter so no cross-run cache tips the scales.
    adapter2 = LocalCodebaseAdapter(root=FIXTURE_ROOT)
    r2 = await L3CallGraphScanner(codebase=adapter2).scan(scan_context_factory(None))
    assert {n.id for n in r1.nodes} == {n.id for n in r2.nodes}
    assert {(e.source_id, e.target_id) for e in r1.edges} == {
        (e.source_id, e.target_id) for e in r2.edges
    }
