"""OpenInference span classification (task 17).

Maps an OpenInference instrumentor's spans (LangChain, LlamaIndex,
Anthropic, OpenAI, ...) onto the same `apv.kind`-driven trace schema
`@trace` produces, with no code change required in the target project
beyond `configure()` plus the instrumentor's own `.instrument()` call.

This module only ever answers "what kind/name does this span resolve to,
given these attributes" -- it never touches a live `Span` object or
decides WHEN to write a line. `_processor.py`'s deferred-emission logic
(D1) owns the timing; this module owns the classification rules it
consults. See `packages/apv_trace/README.md`'s "Using an OpenInference
instrumentor" for the user-facing story.

Attribute key constants verified against the installed
`openinference-semantic-conventions==0.1.41` (`openinference.semconv.
trace.SpanAttributes`/`ToolAttributes`/`OpenInferenceSpanKindValues`) on
2026-10-20 -- see Task 17's Result for the exact values found.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Literal

# Deliberately duplicated from `__init__.py`'s canonical `TraceKind` rather
# than imported from it: `__init__.py` imports `resolve_kind_map` FROM this
# module, so importing `TraceKind` back the other way would be circular.
# Structurally identical `Literal`s are interchangeable to mypy, so this
# costs nothing beyond keeping the two definitions in sync by hand if the
# kind set ever grows again.
TraceKind = Literal["workflow", "agent", "tool", "function", "model"]

OI_SPAN_KIND_ATTR = "openinference.span.kind"

_TOOL_NAME = "tool.name"
_AGENT_NAME = "agent.name"
_GRAPH_NODE_NAME = "graph.node.name"
_LLM_REQUEST_MODEL_NAME = "llm.request.model_name"
_LLM_MODEL_NAME = "llm.model_name"
_METADATA = "metadata"

_VALID_APV_KINDS = frozenset({"workflow", "agent", "tool", "function", "model"})

# Every `openinference.span.kind` value the installed
# `openinference-semantic-conventions==0.1.41` enum defines, verified
# 2026-10-20. Used ONLY to validate override keys typed by a human (a
# config-time check -- catching a typo like `APV_TRACE_OPENINFERENCE_
# KINDS="FOO=tool"`), never to decide whether to classify a real span's
# kind: `classify`'s plain `.get(...)` already handles an unrecognized
# (or future) kind safely by skipping it, with no hazard if this set goes
# stale. This one, unlike `DEFAULT_KIND_MAP`, has to be kept in sync by
# hand if upstream adds a new kind -- accepted, since rejecting an
# override for a kind this list doesn't know about yet is a config-time
# inconvenience, not a silently wrong classification.
_KNOWN_OI_KINDS = frozenset(
    {
        "TOOL",
        "CHAIN",
        "LLM",
        "RETRIEVER",
        "EMBEDDING",
        "AGENT",
        "RERANKER",
        "UNKNOWN",
        "GUARDRAIL",
        "EVALUATOR",
        "PROMPT",
        "DECISION",
    }
)

# Only the OpenInference kinds the default behavior actually maps. Every
# OTHER kind (CHAIN, RETRIEVER, EMBEDDING, RERANKER, GUARDRAIL, EVALUATOR,
# PROMPT, UNKNOWN, and any future value a newer semconv release adds --
# the installed enum already has DECISION, which predates this task but
# isn't in the Design's own table) is skipped by `classify`'s plain
# `.get(...)` returning `None` -- there's no explicit "skip list" here to
# fall out of sync with upstream's enum.
DEFAULT_KIND_MAP: dict[str, TraceKind | None] = {
    "TOOL": "tool",
    "AGENT": "agent",
    "LLM": "model",
}


def classify(oi_kind: str, kind_map: Mapping[str, TraceKind | None]) -> TraceKind | None:
    """Look up the apv kind for an `openinference.span.kind` value, or `None` to skip."""
    return kind_map.get(oi_kind.upper())


def _primary_name_attr(oi_kind: str) -> str:
    """The one attribute whose presence at span START means "classify and emit live" (D1).

    Every kind mapped through the override (not TOOL/AGENT/LLM) uses
    `graph.node.name` -- that's the LangGraph-node-name path, the only
    name source available before a run finishes for an override-mapped
    kind like `CHAIN=workflow`.
    """
    if oi_kind == "TOOL":
        return _TOOL_NAME
    if oi_kind == "AGENT":
        return _AGENT_NAME
    if oi_kind == "LLM":
        return _LLM_REQUEST_MODEL_NAME
    return _GRAPH_NODE_NAME


def is_start_eligible(oi_kind: str, attrs: Mapping[str, Any] | None) -> bool:
    """Whether `on_start` already knows enough to emit a live `start` line (D1)."""
    if attrs is None:
        return False
    value = attrs.get(_primary_name_attr(oi_kind))
    return isinstance(value, str) and bool(value)


def resolve_name(oi_kind: str, attrs: Mapping[str, Any], span_name: str) -> str:
    """The name chain from the Design's kind-map table, evaluated against the FINAL attributes."""
    if oi_kind == "TOOL":
        return _first_str(attrs, _TOOL_NAME) or span_name
    if oi_kind == "AGENT":
        return _first_str(attrs, _AGENT_NAME, _GRAPH_NODE_NAME) or span_name
    if oi_kind == "LLM":
        return _first_str(attrs, _LLM_REQUEST_MODEL_NAME, _LLM_MODEL_NAME) or span_name
    direct = _first_str(attrs, _GRAPH_NODE_NAME)
    if direct:
        return direct
    return _langgraph_node_from_metadata(attrs.get(_METADATA)) or span_name


def _first_str(attrs: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _langgraph_node_from_metadata(raw_metadata: Any) -> str | None:
    """LangGraph puts `langgraph_node` in a run's metadata; the LangChain
    instrumentor copies that run metadata verbatim into the `metadata`
    attribute as a JSON string. Malformed JSON or an unexpected shape is
    silently ignored -- this is a best-effort name hint for an optional
    field, not a misconfiguration worth a `warn` call."""
    if not isinstance(raw_metadata, str) or not raw_metadata:
        return None
    try:
        parsed = json.loads(raw_metadata)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    node_name = parsed.get("langgraph_node")
    return node_name if isinstance(node_name, str) and node_name else None


def parse_kind_overrides(raw: str) -> tuple[dict[str, TraceKind | None], list[str]]:
    """Parse `APV_TRACE_OPENINFERENCE_KINDS="CHAIN=workflow,RETRIEVER=tool,LLM="`.

    Returns `(overrides, rejected_raw_pairs)` -- rejected entries are
    returned rather than warned about here, so the single `warn` call the
    Design asks for ("Invalid entries ... produce one warn") happens once
    per `configure()` call in `resolve_kind_map`, not once per bad pair.
    """
    overrides: dict[str, TraceKind | None] = {}
    rejected: list[str] = []
    for raw_pair in raw.split(","):
        pair = raw_pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            rejected.append(pair)
            continue
        key, _, value = pair.partition("=")
        key = key.strip().upper()
        value = value.strip()
        if not key or key not in _KNOWN_OI_KINDS:
            rejected.append(pair)
            continue
        if not value:
            overrides[key] = None
            continue
        value_lower = value.lower()
        if value_lower not in _VALID_APV_KINDS:
            rejected.append(pair)
            continue
        overrides[key] = value_lower  # type: ignore[assignment]  # checked against _VALID_APV_KINDS above
    return overrides, rejected


def resolve_kind_map(
    *,
    kwarg_overrides: Mapping[str, TraceKind | None] | None,
    env_value: str | None,
    warn: Callable[[str], None],
) -> dict[str, TraceKind | None]:
    """Merge precedence: kwarg, then env, then `DEFAULT_KIND_MAP` -- the
    same order OpenInference uses for its own configuration."""
    merged: dict[str, TraceKind | None] = dict(DEFAULT_KIND_MAP)

    if env_value:
        env_overrides, rejected = parse_kind_overrides(env_value)
        merged.update(env_overrides)
        if rejected:
            warn(f"ignoring malformed APV_TRACE_OPENINFERENCE_KINDS entries: {rejected!r}")

    if kwarg_overrides:
        merged.update({key.upper(): value for key, value in kwarg_overrides.items()})

    return merged
