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
    kind: Literal["workflow", "agent", "tool", "function", "model"],
    phase: Literal["start", "end", "error"] = "start",
    name: str,
    module: str = "pkg.mod",
    qualname: str | None = None,
    db_id: str | None = None,
    framework_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> TraceEvent:
    return TraceEvent(
        v=1,
        ts=_TS,
        run_id="run-1",
        span_id="span-1",
        phase=phase,
        kind=kind,
        ref=TraceRef(name=name, module=module, qualname=qualname, db_id=db_id, framework_id=framework_id),
        meta=meta or {},
    )


def test_resolves_by_db_id() -> None:
    node = _node("mongo:mock_agent.tools:abc123", "tool", "lookup_kb")
    index = NodeIndex.build(Graph(nodes=[node]))

    result = index.resolve(_event(kind="tool", name="lookup_kb", db_id="abc123"))

    assert isinstance(result, Resolved)
    assert result.node_ids == [node.id]
    assert result.strategy == "db_id"


def test_resolves_by_db_id_attribute_for_a_non_mongo_node_id() -> None:
    """Task 9 Scope C: `attributes["db_id"]` must work for ANY connector, not
    just nodes whose `id` happens to be `mongo:...`-prefixed -- the Mongo-id
    parse in `_db_id_of` is a fallback now, not the only path."""
    node = _node(
        "postgres:mock_agent.tools:42", "tool", "lookup_kb", attributes={"db_id": "42"}
    )
    index = NodeIndex.build(Graph(nodes=[node]))

    result = index.resolve(_event(kind="tool", name="lookup_kb", db_id="42"))

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


def test_model_prefix_resolves_a_dated_model_id() -> None:
    node = _node("mongo:mock_agent.models:1", "model", "claude-sonnet-4")
    index = NodeIndex.build(Graph(nodes=[node]))

    result = index.resolve(_event(kind="model", name="claude-sonnet-4-20250514"))

    assert isinstance(result, Resolved)
    assert result.node_ids == [node.id]
    assert result.strategy == "model_prefix"


def test_model_prefix_picks_the_longest_candidate() -> None:
    short = _node("mongo:mock_agent.models:1", "model", "gpt-4o")
    long = _node("mongo:mock_agent.models:2", "model", "gpt-4o-mini")
    index = NodeIndex.build(Graph(nodes=[short, long]))

    result = index.resolve(_event(kind="model", name="gpt-4o-mini-2024-07-18"))

    assert isinstance(result, Resolved)
    assert result.node_ids == [long.id]
    assert result.strategy == "model_prefix"


def test_model_prefix_requires_a_dash_boundary() -> None:
    """`claude-sonnet-45` must NOT match `claude-sonnet-4` -- the dash
    boundary is what tells a dated snapshot apart from an unrelated model
    whose name happens to start the same way."""
    node = _node("mongo:mock_agent.models:1", "model", "claude-sonnet-4")
    index = NodeIndex.build(Graph(nodes=[node]))

    result = index.resolve(_event(kind="model", name="claude-sonnet-45"))

    assert isinstance(result, Unresolved)


def test_model_prefix_is_case_insensitive() -> None:
    node = _node("mongo:mock_agent.models:1", "model", "claude-sonnet-4")
    index = NodeIndex.build(Graph(nodes=[node]))

    result = index.resolve(_event(kind="model", name="CLAUDE-SONNET-4"))

    assert isinstance(result, Resolved)
    assert result.node_ids == [node.id]
    assert result.strategy == "model_prefix"


def test_model_exact_name_still_resolves_through_typed_name_not_prefix() -> None:
    node = _node("mongo:mock_agent.models:1", "model", "claude-sonnet-4")
    index = NodeIndex.build(Graph(nodes=[node]))

    result = index.resolve(_event(kind="model", name="claude-sonnet-4"))

    assert isinstance(result, Resolved)
    assert result.strategy == "typed_name"


def test_model_prefix_ambiguous_when_two_nodes_share_the_matched_name() -> None:
    """Two nodes sharing an EXACT duplicate name hit `by_typed_name`'s own
    ambiguity check before `model_prefix` ever runs (see `resolve`'s
    ordering) -- this test instead forces a tie inside `model_prefix`
    itself: neither node's name exactly equals the ref name, so only the
    PREFIX branch can match, and both duplicates share that prefix name."""
    first = _node("mongo:mock_agent.models:1", "model", "gpt-4-turbo")
    second = _node("mongo:mock_agent.models:2", "model", "gpt-4-turbo")
    index = NodeIndex.build(Graph(nodes=[first, second]))

    result = index.resolve(_event(kind="model", name="gpt-4-turbo-preview"))

    assert isinstance(result, Unresolved)
    assert "ambiguous" in result.reason


def test_tool_kind_events_never_use_model_prefix_matching() -> None:
    model_node = _node("mongo:mock_agent.models:1", "model", "claude-sonnet-4")
    index = NodeIndex.build(Graph(nodes=[model_node]))

    result = index.resolve(_event(kind="tool", name="claude-sonnet-4-20250514"))

    assert isinstance(result, Unresolved)
    assert "no node matches" in result.reason


def test_no_bridge_meta_flag_resolves_the_function_only() -> None:
    """Task 18 Verification 23: `meta.bridge: false` (from `apv-run
    --functions --no-bridge`) must skip the `fn_to_tools` extension and
    report the plain `module_qualname` strategy, not
    `module_qualname+bridge`."""
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
        _event(
            kind="function",
            name="lookup_kb_tool",
            module="runtime_demo.tools",
            qualname="lookup_kb_tool",
            meta={"bridge": False},
        )
    )

    assert isinstance(result, Resolved)
    assert result.node_ids == [fn_node.id]  # only the function -- no tool bridged
    assert result.strategy == "module_qualname"


def test_missing_bridge_meta_key_still_bridges_as_before() -> None:
    """Without the key at all (every pre-task-18 event, and every
    `apv-run --functions` call that didn't pass `--no-bridge`), behavior
    is unchanged."""
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
