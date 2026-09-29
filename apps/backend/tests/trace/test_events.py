"""Schema tests for `TraceEvent`.

These assertions are the actual contract: the exact snake_case JSON shape a
target project's `apv_trace` emitter writes (see
`runtime-demo-project/apv_trace/__init__.py`) must round-trip through
`TraceEvent.model_validate_json` / `model_dump_json` unchanged. A regression
here means the file tailer (task NN+1) will silently drop real events.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agentic_visualizer.trace.events import TraceError, TraceEvent, TraceRef

# A literal example of what `apv_trace._emit` writes to the log file — kept
# in sync with the emitter by hand since the emitter deliberately doesn't
# import this model (see events.py's module docstring).
RAW_START_EVENT = {
    "v": 1,
    "ts": "2026-09-28T12:00:00.123456+00:00",
    "run_id": "01J9Z7QK6H8X2VVYB6R6QYB6R6",
    "span_id": "01J9Z7QK6H8X2VVYB6R6QYB6R7",
    "parent_span_id": None,
    "phase": "start",
    "kind": "tool",
    "ref": {
        "name": "lookup_kb",
        "module": "runtime_demo.tools",
        "qualname": "lookup_kb_tool",
        "framework_id": None,
        "db_id": None,
    },
    "meta": {},
}


def test_round_trip_matches_emitter_shape() -> None:
    """The exact dict `apv_trace` would emit must validate and re-serialize losslessly."""
    event = TraceEvent.model_validate(RAW_START_EVENT)
    assert event.v == 1
    assert event.run_id == RAW_START_EVENT["run_id"]
    assert event.span_id == RAW_START_EVENT["span_id"]
    assert event.parent_span_id is None
    assert event.phase == "start"
    assert event.kind == "tool"
    assert event.ref.name == "lookup_kb"
    assert event.ref.qualname == "lookup_kb_tool"
    assert event.meta == {}

    round_tripped = json.loads(event.model_dump_json())
    assert round_tripped["run_id"] == RAW_START_EVENT["run_id"]
    assert round_tripped["parent_span_id"] is None
    assert round_tripped["ref"]["module"] == "runtime_demo.tools"


def test_json_line_parses_with_model_validate_json() -> None:
    """Exercises the exact call task NN+1's `FileTailTraceSource` makes per line."""
    line = json.dumps(RAW_START_EVENT)
    event = TraceEvent.model_validate_json(line)
    assert event.ref.name == "lookup_kb"


def test_error_event_carries_error_meta() -> None:
    error_event = {
        **RAW_START_EVENT,
        "phase": "error",
        "meta": {
            "error": {
                "type": "RuntimeError",
                "message": "simulated triage failure",
                "traceback": "Traceback (most recent call last):\n...",
            }
        },
    }
    event = TraceEvent.model_validate(error_event)
    assert event.phase == "error"
    # meta stays an open dict on TraceEvent — TraceError is a documentation
    # model, not a nested field type (see events.py). Validate it separately.
    error = TraceError.model_validate(event.meta["error"])
    assert error.type == "RuntimeError"
    assert error.message == "simulated triage failure"


@pytest.mark.parametrize("missing_field", ["run_id", "span_id", "phase", "kind", "ref", "ts"])
def test_missing_required_field_raises(missing_field: str) -> None:
    payload = dict(RAW_START_EVENT)
    del payload[missing_field]
    with pytest.raises(ValidationError):
        TraceEvent.model_validate(payload)


def test_v_defaults_to_1_when_omitted() -> None:
    payload = {k: v for k, v in RAW_START_EVENT.items() if k != "v"}
    event = TraceEvent.model_validate(payload)
    assert event.v == 1


def test_v_rejects_unknown_schema_version() -> None:
    payload = {**RAW_START_EVENT, "v": 2}
    with pytest.raises(ValidationError):
        TraceEvent.model_validate(payload)


@pytest.mark.parametrize("bad_phase", ["started", "END", ""])
def test_invalid_phase_rejected(bad_phase: str) -> None:
    payload = {**RAW_START_EVENT, "phase": bad_phase}
    with pytest.raises(ValidationError):
        TraceEvent.model_validate(payload)


@pytest.mark.parametrize("bad_kind", ["subagent", "task", ""])
def test_invalid_kind_rejected(bad_kind: str) -> None:
    payload = {**RAW_START_EVENT, "kind": bad_kind}
    with pytest.raises(ValidationError):
        TraceEvent.model_validate(payload)


def test_ref_requires_non_empty_name_and_module() -> None:
    with pytest.raises(ValidationError):
        TraceRef.model_validate({"name": "", "module": "runtime_demo.tools"})
    with pytest.raises(ValidationError):
        TraceRef.model_validate({"name": "lookup_kb", "module": ""})


def test_event_is_frozen() -> None:
    event = TraceEvent.model_validate(RAW_START_EVENT)
    with pytest.raises(ValidationError):
        event.phase = "end"  # type: ignore[misc]  # deliberately testing immutability
