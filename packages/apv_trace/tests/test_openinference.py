"""Tests for OpenInference span classification (task 17).

Every test here builds its own local `TracerProvider` and attaches a
`_JsonlSpanProcessor` to it directly — none of this touches
`apv_trace._state` or OTel's global `TracerProvider` singleton at all, so
none of these tests need `test_apv_trace.py`'s `_reset_state` fixture.
That's the whole point of threading `run_id`/`warn` through the
constructor instead of reading `_state` (see `_processor.py`'s module
docstring): the processor is fully testable in isolation.

Verification scenarios map 1:1 to Task 17's own numbered list.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Status, StatusCode

from apv_trace._openinference import DEFAULT_KIND_MAP, resolve_kind_map
from apv_trace._processor import _JsonlSpanProcessor, _ns_to_iso


def _read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _new_processor(
    tmp_path: Path, *, kind_map: dict[str, Any] | None = None
) -> tuple[Path, Any, list[str], _JsonlSpanProcessor]:
    """A processor wired to its own local provider, with nothing global touched."""
    log_path = tmp_path / "trace.jsonl"
    file = open(log_path, "ab", buffering=0)  # noqa: SIM115 - closed via processor.shutdown()
    warnings: list[str] = []
    processor = _JsonlSpanProcessor(
        file,
        run_id="test-run",
        warn=warnings.append,
        openinference_kind_map=kind_map if kind_map is not None else DEFAULT_KIND_MAP,
    )
    provider = TracerProvider()
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("openinference.instrumentation.test")
    return log_path, tracer, warnings, processor


def test_live_tool_start_is_on_disk_mid_span(tmp_path: Path) -> None:
    """Verification 1: Live TOOL."""
    log_path, tracer, _warnings, processor = _new_processor(tmp_path)

    with tracer.start_as_current_span(
        "add", attributes={"openinference.span.kind": "TOOL", "tool.name": "add"}
    ):
        mid_span_events = _read_events(log_path)
        assert len(mid_span_events) == 1
        assert mid_span_events[0]["phase"] == "start"

    events = _read_events(log_path)
    assert [e["phase"] for e in events] == ["start", "end"]
    assert events[0]["kind"] == "tool"
    assert events[0]["ref"]["name"] == "add"
    assert events[0]["ref"]["module"] == "openinference.instrumentation.test"
    assert events[0]["meta"]["source"] == "openinference"
    assert events[0]["meta"]["oi_span_kind"] == "TOOL"
    processor.shutdown()


def test_deferred_tool_langchain_style(tmp_path: Path) -> None:
    """Verification 2: Deferred TOOL (LangChain style) -- nothing at start,
    kind/name set just before the span ends. The start `ts` must equal the
    span's actual start time, not its end time -- `time.sleep` creates a
    gap large enough to tell the two apart."""
    log_path, tracer, _warnings, processor = _new_processor(tmp_path)

    with tracer.start_as_current_span("RunnableSequence") as span:
        assert _read_events(log_path) == []  # nothing known yet -- deferred
        start_time_ns = span.start_time
        time.sleep(0.05)
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("tool.name", "add")

    events = _read_events(log_path)
    assert [e["phase"] for e in events] == ["start", "end"]
    assert events[0]["ts"] == _ns_to_iso(start_time_ns)
    assert events[0]["ts"] != events[1]["ts"]
    processor.shutdown()


def test_deferred_llm_prefers_request_model_name(tmp_path: Path) -> None:
    """Verification 3: Deferred LLM -- kind present at start, but neither
    name attribute is -- not start-eligible, so nothing is written until
    `llm.request.model_name` (and later a dated `llm.model_name`) land."""
    log_path, tracer, _warnings, processor = _new_processor(tmp_path)

    with tracer.start_as_current_span(
        "ChatAnthropic", attributes={"openinference.span.kind": "LLM"}
    ) as span:
        assert _read_events(log_path) == []
        span.set_attribute("llm.request.model_name", "claude-sonnet-4")
        span.set_attribute("llm.model_name", "claude-sonnet-4-20250514")

    events = _read_events(log_path)
    assert events[0]["kind"] == "model"
    assert events[0]["ref"]["name"] == "claude-sonnet-4"
    processor.shutdown()


def test_llm_name_fallback_chain(tmp_path: Path) -> None:
    """Verification 4: LLM name fallbacks."""
    log_path, tracer, _warnings, processor = _new_processor(tmp_path)

    with tracer.start_as_current_span(
        "ChatOpenAI",
        attributes={"openinference.span.kind": "LLM", "llm.model_name": "gpt-4o-mini"},
    ):
        pass
    with tracer.start_as_current_span("Messages", attributes={"openinference.span.kind": "LLM"}):
        pass

    events = _read_events(log_path)
    starts = [e for e in events if e["phase"] == "start"]
    assert starts[0]["ref"]["name"] == "gpt-4o-mini"  # only llm.model_name set
    assert starts[1]["ref"]["name"] == "Messages"  # neither set -- span name
    processor.shutdown()


def test_live_agent_name_stays_the_one_seen_at_start(tmp_path: Path) -> None:
    """Verification 5: Live AGENT stability -- renaming mid-span must not
    change the already-emitted start event's name at end."""
    log_path, tracer, _warnings, processor = _new_processor(tmp_path)

    with tracer.start_as_current_span(
        "LangGraph",
        attributes={"openinference.span.kind": "AGENT", "agent.name": "Research Agent"},
    ) as span:
        span.set_attribute("agent.name", "Renamed Agent")

    events = _read_events(log_path)
    assert events[0]["ref"]["name"] == "Research Agent"
    assert events[1]["ref"]["name"] == "Research Agent"
    processor.shutdown()


def test_chain_skipped_by_default(tmp_path: Path) -> None:
    """Verification 6a: CHAIN is skipped with the default map."""
    log_path, tracer, _warnings, processor = _new_processor(tmp_path)

    with tracer.start_as_current_span("RunnableSequence", attributes={"openinference.span.kind": "CHAIN"}):
        pass

    assert _read_events(log_path) == []
    processor.shutdown()


def test_chain_override_maps_to_workflow_named_by_graph_node_name(tmp_path: Path) -> None:
    """Verification 6b: `CHAIN=workflow` override, named from `graph.node.name`."""
    kind_map = {**DEFAULT_KIND_MAP, "CHAIN": "workflow"}
    log_path, tracer, _warnings, processor = _new_processor(tmp_path, kind_map=kind_map)

    with tracer.start_as_current_span(
        "RunnableSequence",
        attributes={"openinference.span.kind": "CHAIN", "graph.node.name": "agent"},
    ):
        pass

    events = _read_events(log_path)
    assert events[0]["kind"] == "workflow"
    assert events[0]["ref"]["name"] == "agent"
    processor.shutdown()


def test_chain_override_falls_back_to_langgraph_node_metadata(tmp_path: Path) -> None:
    """Verification 6c: no `graph.node.name` -- fall back to `metadata`'s `langgraph_node`."""
    kind_map = {**DEFAULT_KIND_MAP, "CHAIN": "workflow"}
    log_path, tracer, _warnings, processor = _new_processor(tmp_path, kind_map=kind_map)

    with tracer.start_as_current_span(
        "RunnableSequence", attributes={"openinference.span.kind": "CHAIN"}
    ) as span:
        assert _read_events(log_path) == []  # not start-eligible -- deferred
        span.set_attribute("metadata", json.dumps({"langgraph_node": "my_node"}))

    events = _read_events(log_path)
    assert events[0]["ref"]["name"] == "my_node"
    processor.shutdown()


def test_chain_malformed_metadata_json_falls_back_to_span_name(tmp_path: Path) -> None:
    """Verification 6d: malformed `metadata` JSON must not raise."""
    kind_map = {**DEFAULT_KIND_MAP, "CHAIN": "workflow"}
    log_path, tracer, _warnings, processor = _new_processor(tmp_path, kind_map=kind_map)

    with tracer.start_as_current_span(
        "RunnableSequence", attributes={"openinference.span.kind": "CHAIN"}
    ) as span:
        span.set_attribute("metadata", "not valid json")

    events = _read_events(log_path)  # must not raise
    assert events[0]["ref"]["name"] == "RunnableSequence"
    processor.shutdown()


def test_env_override_changes_the_default_map() -> None:
    """Verification 7a: env var overrides apply over the default."""
    warnings: list[str] = []
    kind_map = resolve_kind_map(
        kwarg_overrides=None, env_value="CHAIN=workflow,RETRIEVER=tool", warn=warnings.append
    )
    assert kind_map["CHAIN"] == "workflow"
    assert kind_map["RETRIEVER"] == "tool"
    assert kind_map["TOOL"] == "tool"  # untouched default survives
    assert warnings == []


def test_kwarg_override_beats_env_override() -> None:
    """Verification 7b: the kwarg wins over the env var."""
    kind_map = resolve_kind_map(kwarg_overrides={"LLM": None}, env_value="LLM=tool", warn=lambda _: None)
    assert kind_map["LLM"] is None  # kwarg wins, not "tool" from the env var


def test_invalid_override_entries_warn_once_and_the_rest_still_apply() -> None:
    """Verification 7c: `FOO=tool` (unknown OI kind), `TOOL=banana` (unknown
    apv kind), and `garbage` (malformed pair) are each rejected, with ONE
    aggregate warning -- not one per bad entry -- and `RETRIEVER=tool`
    still applies."""
    warnings: list[str] = []
    kind_map = resolve_kind_map(
        kwarg_overrides=None,
        env_value="FOO=tool,TOOL=banana,garbage,RETRIEVER=tool",
        warn=warnings.append,
    )
    assert kind_map["RETRIEVER"] == "tool"
    assert kind_map["TOOL"] == "tool"  # the invalid TOOL=banana entry never applied
    assert "FOO" not in kind_map
    assert len(warnings) == 1


def test_error_with_exception_event_uses_the_exception(tmp_path: Path) -> None:
    """Verification 8a: status ERROR plus a recorded exception."""
    log_path, tracer, _warnings, processor = _new_processor(tmp_path)

    with pytest.raises(ValueError, match="boom"), tracer.start_as_current_span(
        "add", attributes={"openinference.span.kind": "TOOL", "tool.name": "add"}
    ):
        raise ValueError("boom")

    events = _read_events(log_path)
    assert events[1]["phase"] == "error"
    assert events[1]["meta"]["error"]["type"] == "ValueError"
    assert events[1]["meta"]["error"]["message"] == "boom"
    processor.shutdown()


def test_error_without_exception_event_uses_the_status_description(tmp_path: Path) -> None:
    """Verification 8b: status ERROR with no exception event -- what a
    LangChain chain error looks like (it sets status directly, never
    raises through the span)."""
    log_path, tracer, _warnings, processor = _new_processor(tmp_path)

    with tracer.start_as_current_span(
        "RunnableSequence",
        attributes={"openinference.span.kind": "TOOL", "tool.name": "chain_step"},
    ) as span:
        span.set_status(Status(StatusCode.ERROR, "chain failed"))

    events = _read_events(log_path)
    assert events[1]["phase"] == "error"
    assert events[1]["meta"]["error"]["type"] == "Error"
    assert events[1]["meta"]["error"]["message"] == "chain failed"
    processor.shutdown()


def test_prompt_and_response_attributes_never_reach_the_file(tmp_path: Path) -> None:
    """Verification 9: the processor reads only the allowlisted name
    attributes -- a prompt/response value must never appear in the file,
    not even truncated or nested."""
    marker = "SECRET_PROMPT_MARKER_12345"
    log_path, tracer, _warnings, processor = _new_processor(tmp_path)

    with tracer.start_as_current_span(
        "ChatAnthropic",
        attributes={
            "openinference.span.kind": "LLM",
            "llm.request.model_name": "claude-sonnet-4",
            "input.value": marker,
            "output.value": marker,
            "llm.input_messages.0.message.content": marker,
        },
    ):
        pass

    processor.shutdown()
    assert marker not in log_path.read_text(encoding="utf-8")


def test_live_span_cache_evicts_the_oldest_entry_when_full(tmp_path: Path) -> None:
    """Verification 10: 10,001 live spans that never end -- one eviction
    warning, no crash, size stays at or below the cap."""
    log_path = tmp_path / "trace.jsonl"
    file = open(log_path, "ab", buffering=0)  # noqa: SIM115 - closed below
    warnings: list[str] = []
    processor = _JsonlSpanProcessor(
        file, run_id="test-run", warn=warnings.append, openinference_kind_map=DEFAULT_KIND_MAP
    )
    provider = TracerProvider()
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("openinference.instrumentation.test")

    for i in range(10_001):
        # start_span (not start_as_current_span): creates+starts a span
        # without making it current, and deliberately never ends it --
        # that's the "never finishes" case this cap exists for.
        tracer.start_span(
            f"tool-{i}", attributes={"openinference.span.kind": "TOOL", "tool.name": f"tool-{i}"}
        )

    assert len(processor._live) <= 10_000
    assert len(warnings) == 1
    processor.shutdown()


def test_unrelated_span_is_skipped_and_logged_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Verification 11: a span with neither `apv.kind` nor
    `openinference.span.kind` is skipped, with a debug log."""
    log_path, tracer, _warnings, processor = _new_processor(tmp_path)

    with caplog.at_level("DEBUG", logger="apv_trace"), tracer.start_as_current_span("unrelated_http_call"):
        pass

    assert _read_events(log_path) == []
    assert any("unrelated_http_call" in record.message for record in caplog.records)
    processor.shutdown()


def test_apv_kind_wins_when_both_attributes_are_present(tmp_path: Path) -> None:
    """Verification 12: `apv.kind` wins over `openinference.span.kind`."""
    log_path, tracer, _warnings, processor = _new_processor(tmp_path)

    with tracer.start_as_current_span(
        "weird",
        attributes={
            "apv.kind": "tool",
            "apv.name": "real_tool",
            "apv.module": "some.module",
            "apv.qualname": "real_tool",
            "openinference.span.kind": "AGENT",
            "agent.name": "wrong",
        },
    ):
        pass

    events = _read_events(log_path)
    assert events[0]["kind"] == "tool"
    assert events[0]["ref"]["name"] == "real_tool"
    processor.shutdown()


def test_langchain_instrumentor_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification 13: real LangChain instrumentor, no network.

    Confirmed against the installed `openinference-instrumentation-
    langchain==0.1.79` on 2026-10-20: a bare `tool.invoke(...)` call
    produces exactly one TOOL span named after the tool itself (`"add"`),
    with `openinference.span.kind`/`tool.name` set only at `on_end` (no
    attributes at all at `on_start`) -- the deferred path this task adds,
    exercised end to end with zero synthetic spans.
    """
    pytest.importorskip("openinference.instrumentation.langchain")
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

    from langchain_core.tools import tool as langchain_tool
    from openinference.instrumentation.langchain import LangChainInstrumentor

    log_path = tmp_path / "trace.jsonl"
    file = open(log_path, "ab", buffering=0)  # noqa: SIM115 - closed via processor.shutdown()
    warnings: list[str] = []
    processor = _JsonlSpanProcessor(
        file, run_id="test-run", warn=warnings.append, openinference_kind_map=DEFAULT_KIND_MAP
    )
    provider = TracerProvider()
    provider.add_span_processor(processor)

    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:

        @langchain_tool
        def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        result = add.invoke({"a": 1, "b": 2})
        assert result == 3
    finally:
        instrumentor.uninstrument()
        processor.shutdown()

    events = _read_events(log_path)
    tool_events = [e for e in events if e["ref"]["name"] == "add"]
    assert [e["phase"] for e in tool_events] == ["start", "end"]
    assert all(e["kind"] == "tool" for e in tool_events)
