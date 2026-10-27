"""One test per `span_mapping.classify` rule, plus rule-order and no-leak checks (Task 23)."""

from __future__ import annotations

from agentic_visualizer.runtime.span_mapping import classify


def test_rule1_apv_kind_builds_ref_from_apv_attributes() -> None:
    attrs = {
        "apv.kind": "tool",
        "apv.name": "search_docs",
        "apv.module": "mock_agent.tools",
        "apv.qualname": "search_docs",
    }
    result = classify(attrs, span_name="irrelevant", scope_name=None)

    assert result is not None
    assert result.kind == "tool"
    assert result.ref == {
        "name": "search_docs",
        "module": "mock_agent.tools",
        "qualname": "search_docs",
        "framework_id": None,
        "db_id": None,
    }


def test_rule1_apv_kind_invalid_value_is_committed_to_none() -> None:
    """An unrecognized apv.kind value is a committed None, not a fall-through
    to the other rules -- even if the span also carries other attributes."""
    attrs = {"apv.kind": "not-a-real-kind", "openinference.span.kind": "TOOL", "tool.name": "x"}

    assert classify(attrs, span_name="x", scope_name=None) is None


def test_rule2_openinference_tool() -> None:
    attrs = {"openinference.span.kind": "TOOL", "tool.name": "web_search"}
    result = classify(attrs, span_name="span-name", scope_name="my.instrumentor")

    assert result is not None
    assert result.kind == "tool"
    assert result.ref["name"] == "web_search"
    assert result.ref["module"] == "my.instrumentor"


def test_rule2_openinference_agent_falls_back_to_graph_node_name() -> None:
    attrs = {"openinference.span.kind": "AGENT", "graph.node.name": "planner"}
    result = classify(attrs, span_name="span-name", scope_name=None)

    assert result is not None
    assert result.kind == "agent"
    assert result.ref["name"] == "planner"
    assert result.ref["module"] == "openinference"  # no scope -- falls back


def test_rule2_openinference_llm_name_chain() -> None:
    attrs = {"openinference.span.kind": "LLM", "llm.model_name": "gpt-4o"}
    result = classify(attrs, span_name="span-name", scope_name=None)

    assert result is not None
    assert result.kind == "model"
    assert result.ref["name"] == "gpt-4o"


def test_rule2_unmapped_openinference_kind_is_committed_to_none() -> None:
    """CHAIN isn't in the default kind map -- committed None, no fall-through
    to GenAI/code-attribute rules."""
    attrs = {"openinference.span.kind": "CHAIN", "gen_ai.operation.name": "chat"}

    assert classify(attrs, span_name="x", scope_name=None) is None


def test_rule3_genai_invoke_agent() -> None:
    attrs = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "triage"}
    result = classify(attrs, span_name="span-name", scope_name=None)

    assert result is not None
    assert result.kind == "agent"
    assert result.ref["name"] == "triage"


def test_rule3_genai_execute_tool() -> None:
    attrs = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_order"}
    result = classify(attrs, span_name="span-name", scope_name=None)

    assert result is not None
    assert result.kind == "tool"
    assert result.ref["name"] == "lookup_order"


def test_rule3_genai_chat_uses_request_model_then_response_model() -> None:
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.response.model": "claude-sonnet-5-20261001",
        "gen_ai.request.model": "claude-sonnet-5",
    }
    result = classify(attrs, span_name="span-name", scope_name=None)

    assert result is not None
    assert result.kind == "model"
    assert result.ref["name"] == "claude-sonnet-5"  # request.model wins over response.model


def test_rule3_genai_unmapped_operation_is_none() -> None:
    attrs = {"gen_ai.operation.name": "embeddings"}

    assert classify(attrs, span_name="x", scope_name=None) is None


def test_rule4_code_function_name_stable_attribute() -> None:
    attrs = {"code.function.name": "mock_agent.tools.search_docs"}
    result = classify(attrs, span_name="span-name", scope_name=None)

    assert result is not None
    assert result.kind == "function"
    assert result.ref == {
        "name": "search_docs",
        "module": "mock_agent.tools",
        "qualname": "search_docs",
        "framework_id": None,
        "db_id": None,
    }


def test_rule4_code_function_name_ending_in_dot_is_none_not_empty_name() -> None:
    """A malformed `code.function.name` with a trailing dot would otherwise
    produce an empty `qualname`/`name` -- `TraceRef.name` requires at
    least one character, so this must commit to None rather than build a
    ref `TraceRef(**ref)` would reject."""
    attrs = {"code.function.name": "pkg.mod.Class."}

    assert classify(attrs, span_name="x", scope_name=None) is None


def test_non_string_optional_attribute_is_dropped_not_passed_through() -> None:
    """`apv.framework_id` set to a non-string OTel attribute value (bool,
    here) must not reach `TraceRef` as-is -- `_make_ref` drops it to None
    rather than letting `TraceRef(**ref)` raise on an optional field."""
    attrs = {
        "apv.kind": "tool",
        "apv.name": "search_docs",
        "apv.module": "mock_agent.tools",
        "apv.framework_id": True,  # wrong type -- should become None, not raise
    }
    result = classify(attrs, span_name="x", scope_name=None)

    assert result is not None
    assert result.ref["framework_id"] is None


def test_rule4_code_function_and_namespace_incubating_pair() -> None:
    attrs = {"code.function": "search_docs", "code.namespace": "mock_agent.tools"}
    result = classify(attrs, span_name="span-name", scope_name=None)

    assert result is not None
    assert result.kind == "function"
    assert result.ref["module"] == "mock_agent.tools"
    assert result.ref["qualname"] == "search_docs"


def test_rule5_no_recognized_attributes_is_none() -> None:
    attrs = {"http.method": "GET", "http.route": "/healthz"}

    assert classify(attrs, span_name="GET /healthz", scope_name="fastapi") is None


def test_rule_order_apv_kind_wins_over_openinference() -> None:
    attrs = {
        "apv.kind": "agent",
        "apv.name": "planner",
        "apv.module": "mock_agent",
        "openinference.span.kind": "TOOL",
        "tool.name": "should-be-ignored",
    }
    result = classify(attrs, span_name="x", scope_name=None)

    assert result is not None
    assert result.kind == "agent"
    assert result.ref["name"] == "planner"


def test_prompt_and_response_content_never_leaks_into_ref_or_meta() -> None:
    attrs = {
        "openinference.span.kind": "LLM",
        "llm.model_name": "gpt-4o",
        "input.value": "the user's secret prompt",
        "output.value": "the model's full response text",
        "llm.prompts": ["system prompt here"],
    }
    result = classify(attrs, span_name="x", scope_name=None)

    assert result is not None
    serialized = repr(result.ref) + repr(result.meta)
    assert "secret prompt" not in serialized
    assert "full response text" not in serialized
    assert "system prompt here" not in serialized
