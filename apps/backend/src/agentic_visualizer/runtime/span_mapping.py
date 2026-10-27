"""Backend-side span classification for the OTLP/HTTP trace method (Task 23).

`OtlpHttpTraceSource` receives raw OTel spans from a target project's own
exporter -- unlike the file-tail method, there's no `apv_trace` emitter in
the loop to decide `kind`/`ref` ahead of time. This module does that
classification here instead, from the span's merged resource+span
attributes (span wins), plus the scope's own *name* (not its attributes --
see the task's own "Converting each span" step 1) as a `ref.module`
fallback for rules 2 and 3.

**Why OpenInference classification is duplicated, not imported.** Rule 2
below re-implements `packages/apv_trace/src/apv_trace/_openinference.py`'s
default kind map and name chains. The app can't import `apv_trace`
(CLAUDE.md §12: it ships with target projects, never as a dependency of
this app) -- so the only way to recognize an OpenInference instrumentor's
spans here is a second, backend-owned copy of the same rules.
`tests/runtime/test_span_mapping_parity.py` pins the two together by
asserting they classify the same fixtures identically; this app's dev
dependencies already include `apv-trace` for exactly that test (see
`pyproject.toml`'s `[dependency-groups.dev]`), which is otherwise unused by
non-test code.

**Rule order is "first matching attribute wins," not "first rule that
produces a result."** If a span carries `apv.kind` but it's not one of the
five recognized kinds, this returns `None` rather than falling through to
OpenInference/GenAI/code-attribute rules -- a span explicitly tagged by one
scheme is never reinterpreted under another.

**Never surface prompt/response content.** `ref`/`meta` here are built only
from the name-identifying attributes each rule lists (tool/agent/model
names, code symbol names) -- never from `input.value`/`output.value`,
`gen_ai.prompt*`/`gen_ai.completion*`, or any other message-content
attribute. Those exist on real spans from LLM instrumentors but this module
never reads them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

TraceKind = Literal["workflow", "agent", "tool", "function", "model"]

_VALID_KINDS = frozenset({"workflow", "agent", "tool", "function", "model"})

# Ported from `apv_trace._openinference.DEFAULT_KIND_MAP`, verified against
# `openinference-semantic-conventions==0.1.41` on 2026-10-20 (see Task 17's
# Result). The backend doesn't support the `APV_TRACE_OPENINFERENCE_KINDS`
# override env var -- that's a target-side, `apv_trace`-specific knob with
# no equivalent setting on this method -- so only the three default,
# unconditionally-mapped kinds are recognized here.
_OI_KIND_MAP: dict[str, TraceKind] = {
    "TOOL": "tool",
    "AGENT": "agent",
    "LLM": "model",
}

# OTel GenAI semantic conventions (`github.com/open-telemetry/semantic-conventions-genai`,
# status: Development -- unstable, may rename). Verified against that repo's
# `docs/gen-ai/gen-ai-spans.md` and `docs/gen-ai/gen-ai-agent-spans.md` on
# 2026-10-22; see this task's Result for the exact commit/version checked.
_GENAI_AGENT_OPERATIONS = frozenset({"invoke_agent", "create_agent"})
_GENAI_MODEL_OPERATIONS = frozenset({"chat", "text_completion", "generate_content"})

_FALLBACK_MODULE_OPENINFERENCE = "openinference"
_FALLBACK_MODULE_GENAI = "gen_ai"
_FALLBACK_MODULE_CODE = "code"


@dataclass(frozen=True, slots=True)
class Classified:
    """One span's classification: enough to build a `start`/`end` `TraceEvent` pair."""

    kind: TraceKind
    ref: dict[str, Any]
    meta: dict[str, Any]


def classify(attrs: Mapping[str, Any], span_name: str, scope_name: str | None) -> Classified | None:
    """Classify one span's merged attributes. `None` means "not a span this
    app's graph can resolve against" -- the caller counts that as
    `events_ignored`, not a dead letter (see `sources/otlp_http.py`)."""
    if "apv.kind" in attrs:
        return _classify_apv(attrs)

    oi_kind_raw = attrs.get("openinference.span.kind")
    if isinstance(oi_kind_raw, str):
        return _classify_openinference(oi_kind_raw, attrs, span_name, scope_name)

    operation = attrs.get("gen_ai.operation.name")
    if isinstance(operation, str):
        return _classify_genai(operation, attrs, span_name, scope_name)

    return _classify_code_attributes(attrs, scope_name)


def _classify_apv(attrs: Mapping[str, Any]) -> Classified | None:
    """Rule 1: `apv.kind` -- spans from `apv_trace`'s `@trace`, or a raw-OTel
    emitter that sets the same `apv.*` attributes by hand (e.g.
    `runtime-demo-project`'s OTLP-only demo). Mirrors `apv_trace._processor.
    _ref_from_attributes` field-for-field."""
    kind = attrs.get("apv.kind")
    if kind not in _VALID_KINDS:
        return None
    name = attrs.get("apv.name")
    module = attrs.get("apv.module")
    if not isinstance(name, str) or not name or not isinstance(module, str) or not module:
        return None
    ref = _make_ref(
        name,
        module,
        qualname=attrs.get("apv.qualname"),
        framework_id=attrs.get("apv.framework_id"),
        db_id=attrs.get("apv.db_id"),
    )
    return Classified(kind=kind, ref=ref, meta={})


def _classify_openinference(
    oi_kind_raw: str, attrs: Mapping[str, Any], span_name: str, scope_name: str | None
) -> Classified | None:
    """Rule 2: `openinference.span.kind` -- ported name chains from
    `apv_trace._openinference.resolve_name`. Only TOOL/AGENT/LLM map to
    anything (see `_OI_KIND_MAP`'s own docstring); every other OpenInference
    kind (CHAIN, RETRIEVER, ...) is a committed `None` here, not a
    fall-through to GenAI/code-attribute rules -- the span already
    identified itself as OpenInference's.
    """
    kind = _OI_KIND_MAP.get(oi_kind_raw.upper())
    if kind is None:
        return None
    if kind == "tool":
        name = _first_str(attrs, "tool.name") or span_name
    elif kind == "agent":
        name = _first_str(attrs, "agent.name", "graph.node.name") or span_name
    else:  # kind == "model" (LLM)
        name = _first_str(attrs, "llm.request.model_name", "llm.model_name") or span_name
    ref = _make_ref(name, scope_name or _FALLBACK_MODULE_OPENINFERENCE)
    return Classified(kind=kind, ref=ref, meta={})


def _classify_genai(
    operation: str, attrs: Mapping[str, Any], span_name: str, scope_name: str | None
) -> Classified | None:
    """Rule 3: `gen_ai.operation.name` (OTel GenAI semantic conventions)."""
    module = scope_name or _FALLBACK_MODULE_GENAI
    if operation in _GENAI_AGENT_OPERATIONS:
        name = _first_str(attrs, "gen_ai.agent.name") or span_name
        return Classified(kind="agent", ref=_make_ref(name, module), meta={})
    if operation == "execute_tool":
        name = _first_str(attrs, "gen_ai.tool.name") or span_name
        return Classified(kind="tool", ref=_make_ref(name, module), meta={})
    if operation in _GENAI_MODEL_OPERATIONS:
        name = _first_str(attrs, "gen_ai.request.model", "gen_ai.response.model") or span_name
        return Classified(kind="model", ref=_make_ref(name, module), meta={})
    return None


def _classify_code_attributes(attrs: Mapping[str, Any], scope_name: str | None) -> Classified | None:
    """Rule 4: code attributes. `code.function.name` is the current stable
    attribute (promoted in semconv v1.33.0) -- a fully-qualified name
    without arguments, e.g. `"pkg.mod.Class.method"`; everything before the
    last dot is the module, the last segment is both name and qualname.
    `code.function` + `code.namespace` is the older, removed-from-stable
    pair some libraries still emit -- checked second, per the task's own
    "check both forms" note.
    """
    fq_name = attrs.get("code.function.name")
    if isinstance(fq_name, str) and fq_name:
        module, _, qualname = fq_name.rpartition(".")
        if not module:
            module = scope_name or _FALLBACK_MODULE_CODE
        if not qualname:
            # A fully-qualified name ending in "." (e.g. a malformed
            # "pkg.Cls.") would otherwise produce an empty `name` --
            # `TraceRef.name` requires at least one character.
            return None
        ref = _make_ref(qualname, module, qualname=qualname)
        return Classified(kind="function", ref=ref, meta={})

    function = attrs.get("code.function")
    namespace = attrs.get("code.namespace")
    if isinstance(function, str) and function and isinstance(namespace, str) and namespace:
        ref = _make_ref(function, namespace, qualname=function)
        return Classified(kind="function", ref=ref, meta={})

    return None


def _make_ref(name: str, module: str, *, qualname: Any = None, framework_id: Any = None, db_id: Any = None) -> dict[str, Any]:
    """Build the `ref` dict every rule hands to `TraceRef(**ref)`.

    `qualname`/`framework_id`/`db_id` are optional `str | None` fields on
    `TraceRef` -- anything that isn't a string (e.g. a target setting
    `apv.framework_id` as a bool/int attribute, a legal OTel value kind
    `_attrs_to_dict` happily passes through) is dropped to `None` here
    rather than handed through as-is. Without this, one malformed OPTIONAL
    attribute on an otherwise-valid span would raise a Pydantic
    `ValidationError` out of `TraceRef(**ref)` and -- absent the per-span
    try/except in `otlp_http.py`'s `_handle_export` -- take the rest of
    that export's spans down with it. `name`/`module` themselves are NOT
    re-validated here: every caller already checked those are non-empty
    strings before calling this (they're required, not optional).
    """
    return {
        "name": name,
        "module": module,
        "qualname": qualname if isinstance(qualname, str) else None,
        "framework_id": framework_id if isinstance(framework_id, str) else None,
        "db_id": db_id if isinstance(db_id, str) else None,
    }


def _first_str(attrs: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, str) and value:
            return value
    return None
