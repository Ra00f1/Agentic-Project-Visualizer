"""Pins `span_mapping.classify`'s OpenInference rule (rule 2) against the
real `apv_trace._openinference` module it was ported from (Task 23's own
"Risk: the OpenInference mapping is now in two places" -- this is the test
that keeps them honest).

Only `kind` and the resolved `name` are compared, per the task's own Design
("asserts that both classify the same ... fixtures identically: kind and
name") -- `ref.module`/`qualname` legitimately differ (this module takes a
plain `scope_name: str | None`; `apv_trace` reads a live OTel
`ReadableSpan`'s `instrumentation_scope`), so comparing those would be
testing an intentional difference, not a regression.
"""

from __future__ import annotations

import pytest
from apv_trace._openinference import DEFAULT_KIND_MAP
from apv_trace._openinference import classify as apv_trace_classify
from apv_trace._openinference import resolve_name as apv_trace_resolve_name

from agentic_visualizer.runtime.span_mapping import classify

_FIXTURES: list[dict[str, object]] = [
    {"openinference.span.kind": "TOOL", "tool.name": "web_search"},
    {"openinference.span.kind": "TOOL"},  # no tool.name -- falls back to span_name
    # Deliberately NOT testing a lowercase kind like "tool" here: apv_trace's own
    # `classify()` uppercases before its kind-map lookup, but its `resolve_name()`
    # branches on an exact-case match ("TOOL"/"AGENT"/"LLM") -- a real
    # inconsistency in apv_trace itself (every real OpenInference instrumentor
    # emits the canonical uppercase form, so it's never hit in practice). This
    # backend module normalizes case once and reuses it for both the kind lookup
    # and the name chain, so it would disagree with apv_trace.resolve_name on
    # that specific input -- not a backend bug to chase, just not a fixture this
    # parity test should assert on.
    {"openinference.span.kind": "AGENT", "agent.name": "planner"},
    {"openinference.span.kind": "AGENT", "graph.node.name": "fallback_node"},  # no agent.name
    {"openinference.span.kind": "AGENT"},  # neither -- falls back to span_name
    {"openinference.span.kind": "AGENT", "agent.name": "planner", "graph.node.name": "ignored"},
    {"openinference.span.kind": "LLM", "llm.request.model_name": "gpt-4o"},
    {"openinference.span.kind": "LLM", "llm.model_name": "gpt-4o-fallback"},
    {
        "openinference.span.kind": "LLM",
        "llm.request.model_name": "primary",
        "llm.model_name": "secondary",
    },
    {"openinference.span.kind": "LLM"},  # neither -- falls back to span_name
    {"openinference.span.kind": "CHAIN"},  # unmapped -- both None
    {"openinference.span.kind": "RETRIEVER"},  # unmapped -- both None
    {"openinference.span.kind": "EMBEDDING"},  # unmapped -- both None
    {"openinference.span.kind": "GUARDRAIL"},  # unmapped -- both None
    {"openinference.span.kind": "UNKNOWN"},  # unmapped -- both None
]


@pytest.mark.parametrize("attrs", _FIXTURES)
def test_backend_and_apv_trace_agree_on_kind_and_name(attrs: dict[str, object]) -> None:
    span_name = "the-span-name"
    oi_kind_raw = str(attrs["openinference.span.kind"])

    backend_result = classify(attrs, span_name=span_name, scope_name=None)

    apv_kind = apv_trace_classify(oi_kind_raw, DEFAULT_KIND_MAP)
    if apv_kind is None:
        assert backend_result is None
        return

    assert backend_result is not None
    assert backend_result.kind == apv_kind
    assert backend_result.ref["name"] == apv_trace_resolve_name(oi_kind_raw, attrs, span_name)
