"""Unit tests for NodeIndex.build/resolve -- every strategy, including bridge and ambiguity.

Graphs are built by hand rather than via a live scan: NodeIndex only cares
about Node/Edge shapes, not where they came from, and hand-built graphs make
each strategy's trigger condition explicit and independent of the fixture DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from agentic_visualizer.domain import Edge, Graph, Node, Provenance
from agentic_visualizer.domain.edge import EdgeKind
from agentic_visualizer.domain.node import NodeType
from agentic_visualizer.runtime.node_index import NodeIndex, Resolved, Unresolved
from agentic_visualizer.trace.events import TraceEvent, TraceRef

_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _prov(source: str = "test", source_ref: str = "test:1") -> Provenance:
    return Provenance(source=source, source_ref=source_ref, scanned_at=_TS)


def _node(node_id: str, node_type: NodeType, name: str, attributes: dict[str, Any] | None = None) -> Node:
    return Node(id=node_id, type=node_type, name=name, provenance=_prov(), attributes=attributes or {})


def _edge(source_id: str, target_id: str, kind: EdgeKind, attributes: dict[str, Any] | None = None) -> Edge:
    return Edge(source_id=source_id, target_id=target_id, kind=kind, provenance=_prov(), attributes=attributes or {})


def _event(
    *,
    kind: Literal["workflow", "agent", "tool", "function"],
    phase: Literal["start", "end", "error"] = "start",
    name: str,
    module: str = "pkg.mod",
    qualname: str | None = None,
    db_id: str | None = None,
    framework_id: str | None = None,
) -> TraceEvent:
    return TraceEvent(
        v=1,
        ts=_TS,
        run_id="run-1",
        span_id="span-1",
        phase=phase,
        kind=kind,
        ref=TraceRef(name=name, module=module, qualname=qualname, db_id=db_id, framework_id=framework_id),
    )


def test_resolves_by_db_id() -> None:
    node = _node("mongo:mock_agent.tools:abc123", "tool", "lookup_kb")
    index = NodeIndex.build(Graph(nodes=[node]))

    result = index.resolve(_event(kind="tool", name="lookup_kb", db_id="abc123"))

    assert isinstance(result, Resolved)
    assert result.node_ids == [node.id]
    assert result.strategy == "db_id"


def test_resolves_by_framework_id() -> None:
    node = _node("code:x.py:1:foo", "tool", "foo_tool", attributes={"framework_id": "fw-42"})
    index = NodeIndex.build(Graph(nodes=[node]))

    result = index.resolve(_event(kind="tool", name="foo_tool", framework_id="fw-42"))

    assert isinstance(result, Resolved)
    assert result.node_ids == [node.id]
    assert result.strategy == "framework_id"


def test_resolves_by_module_qualname_for_function_node() -> None:
    fn_node = _node(
        "code:runtime_demo/tools.py:10:lookup_kb_tool",
        "function",
        "lookup_kb_tool",
        attributes={"file_path": "runtime_demo/tools.py", "qualname": "lookup_kb_tool"},
    )
    index = NodeIndex.build(Graph(nodes=[fn_node]))

    result = index.resolve(
        _event(kind="function", name="lookup_kb_tool", module="runtime_demo.tools", qualname="lookup_kb_tool")
    )

    assert isinstance(result, Resolved)
    assert result.node_ids == [fn_node.id]
    assert result.strategy == "module_qualname+bridge"


def test_bridges_to_the_single_tool_that_implements_the_function() -> None:
    fn_node = _node(
        "code:runtime_demo/tools.py:10:lookup_kb_tool",
        "function",
        "lookup_kb_tool",
        attributes={"file_path": "runtime_demo/tools.py", "qualname": "lookup_kb_tool"},
    )
    tool_node = _node("mongo:mock_agent.tools:abc", "tool", "lookup_kb")
    edge = _edge(tool_node.id, fn_node.id, "implements")
    index = NodeIndex.build(Graph(nodes=[fn_node, tool_node], edges=[edge]))

    result = index.resolve(
        _event(kind="function", name="lookup_kb_tool", module="runtime_demo.tools", qualname="lookup_kb_tool")
    )

    assert isinstance(result, Resolved)
    assert set(result.node_ids) == {fn_node.id, tool_node.id}
    assert result.strategy == "module_qualname+bridge"


def test_bridge_suppressed_when_multiple_tools_implement_the_same_function() -> None:
    """Task 3's Risk note: ambiguous ownership must not light up an arbitrary tool."""
    fn_node = _node(
        "code:shared.py:1:shared_fn",
        "function",
        "shared_fn",
        attributes={"file_path": "shared.py", "qualname": "shared_fn"},
    )
    tool_a = _node("mongo:mock_agent.tools:a", "tool", "tool_a")
    tool_b = _node("mongo:mock_agent.tools:b", "tool", "tool_b")
    edges = [_edge(tool_a.id, fn_node.id, "implements"), _edge(tool_b.id, fn_node.id, "implements")]
    index = NodeIndex.build(Graph(nodes=[fn_node, tool_a, tool_b], edges=edges))

    result = index.resolve(_event(kind="function", name="shared_fn", module="shared", qualname="shared_fn"))

    assert isinstance(result, Resolved)
    assert result.node_ids == [fn_node.id]  # only the function -- no tool bridged


def test_resolves_by_tool_fn_when_module_qualname_does_not_match() -> None:
    tool_node = _node(
        "mongo:mock_agent.tools:xyz",
        "tool",
        "lookup_kb",
        attributes={"function_name": "lookup_kb_tool", "module_path": "runtime_demo.tools"},
    )
    index = NodeIndex.build(Graph(nodes=[tool_node]))

    # kind="tool" events never hit by_module_qualname (only function-typed
    # nodes are indexed there), so this exercises strategy 4 directly.
    result = index.resolve(
        _event(kind="tool", name="lookup_kb_tool", module="runtime_demo.tools", qualname="lookup_kb_tool")
    )

    assert isinstance(result, Resolved)
    assert result.node_ids == [tool_node.id]
    assert result.strategy == "tool_fn"


def test_tool_fn_key_prefers_import_path_over_disagreeing_function_name_fields() -> None:
    """Regression test: must match L2's own precedence (import_path wins) --
    getting this backwards silently breaks resolution when a tool record's
    import_path and separate function_name/module_path fields disagree
    (e.g. a bound method, where import_path includes the class segment)."""
    tool_node = _node(
        "mongo:mock_agent.tools:xyz",
        "tool",
        "load_tool",
        attributes={
            "import_path": "src.tools.class_tools.ClassTool.load",
            # Deliberately disagreeing / stale pair -- L2 would ignore these
            # once import_path is present, so this index must too.
            "function_name": "load",
            "module_path": "src.tools.class_tools",
        },
    )
    index = NodeIndex.build(Graph(nodes=[tool_node]))

    # import_path.rpartition(".") splits on the LAST dot: ("load", "src.tools.class_tools.ClassTool").
    # A ref shaped like that parse must resolve -- proving import_path won,
    # not the disagreeing function_name/module_path pair (which would have
    # produced the key ("load", "src.tools.class_tools") instead -- no
    # "ClassTool" segment -- and NOT matched this ref).
    result = index.resolve(
        _event(kind="tool", name="load", module="src.tools.class_tools.ClassTool")
    )
    assert isinstance(result, Resolved)
    assert result.node_ids == [tool_node.id]
    assert result.strategy == "tool_fn"


def test_resolves_by_typed_name_when_unambiguous() -> None:
    node = _node("mongo:mock_agent.agents:1", "agent", "TriageAgent")
    index = NodeIndex.build(Graph(nodes=[node]))

    result = index.resolve(_event(kind="agent", name="TriageAgent"))

    assert isinstance(result, Resolved)
    assert result.node_ids == [node.id]
    assert result.strategy == "typed_name"


def test_typed_name_ambiguous_is_unresolved() -> None:
    a = _node("mongo:mock_agent.agents:1", "agent", "Dup")
    b = _node("mongo:mock_agent.agents:2", "agent", "Dup")
    index = NodeIndex.build(Graph(nodes=[a, b]))

    result = index.resolve(_event(kind="agent", name="Dup"))

    assert isinstance(result, Unresolved)
    assert "ambiguous" in result.reason


def test_no_match_is_unresolved() -> None:
    index = NodeIndex.build(Graph())

    result = index.resolve(_event(kind="tool", name="nothing_matches_this"))

    assert isinstance(result, Unresolved)
    assert "no node matches" in result.reason


def test_strategy_order_prefers_db_id_over_typed_name() -> None:
    """Both a db_id match and a typed_name match exist -- db_id must win (it's tried first)."""
    correct = _node("mongo:mock_agent.tools:right", "tool", "ambiguous_name")
    decoy = _node("mongo:mock_agent.tools:wrong", "tool", "ambiguous_name")
    index = NodeIndex.build(Graph(nodes=[correct, decoy]))

    result = index.resolve(_event(kind="tool", name="ambiguous_name", db_id="right"))

    assert isinstance(result, Resolved)
    assert result.node_ids == [correct.id]
    assert result.strategy == "db_id"
