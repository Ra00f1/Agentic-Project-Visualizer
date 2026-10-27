"""Tests for the trace-method registry (`runtime/methods/`) and
`RuntimeChannel.apply_config`'s start/stop/restart diffing (Task 22).

Uses local fake `TraceMethod`s (not `tests.conftest.FakeTraceMethod`,
which only wraps one fixed source) since these tests need to observe HOW
MANY times `create_source` was called and with what source each time --
exactly the instrumentation `apply_config`'s diffing logic needs covered.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from agentic_visualizer.runtime.channel import MethodConfig, RuntimeChannel
from agentic_visualizer.runtime.methods import TRACE_METHODS
from agentic_visualizer.runtime.methods.base import TraceMethodInfo
from agentic_visualizer.trace.events import TraceEvent, TraceRef
from tests.conftest import ScriptedTraceSource as _ScriptedSource

_TS = datetime(2026, 1, 1, tzinfo=UTC)
_SUPPORTED_SCHEMA_TYPES = {"string", "integer", "boolean"}


def _event(name: str = "foo") -> TraceEvent:
    return TraceEvent(
        v=1, ts=_TS, run_id="r1", span_id="s1", phase="start", kind="tool",
        ref=TraceRef(name=name, module="pkg.mod"),
    )


# --- Registry (Verification 1) ---------------------------------------------


def test_apv_live_file_is_registered_with_a_warning_and_live_state() -> None:
    method = TRACE_METHODS["apv_live_file"]
    info = method.info()
    assert info.id == "apv_live_file"
    assert info.warning is not None
    assert info.live_state is True


def test_every_registered_methods_settings_schema_is_flat_primitives() -> None:
    """The frontend's `SchemaFields` only renders flat `string`/`integer`/
    `boolean` properties (Task 22's own Design: "flat fields only") -- a
    future method with a nested object or array setting would silently
    fail to render. This is the backstop that catches it at registration
    time instead."""
    for method_id, method in TRACE_METHODS.items():
        schema = method.info().settings_schema
        properties = schema.get("properties", {})
        assert isinstance(properties, dict)
        for field_name, field_schema in properties.items():
            field_type = field_schema.get("type")
            assert field_type in _SUPPORTED_SCHEMA_TYPES, (
                f"{method_id}.{field_name} has unsupported schema type {field_type!r} -- "
                f"SchemaFields can only render string/integer/boolean"
            )


# --- apply_config: start / stop / restart / failure isolation -------------


class _Settings(BaseModel):
    tag: str = ""


class _SpyMethod:
    """Records every `TraceSource` it creates (in order), so a test can
    assert exactly how many times -- and with what events -- `apply_config`
    actually (re)started this method."""

    settings_model: type[BaseModel] = _Settings

    def __init__(self, method_id: str, *, events: list[TraceEvent] | None = None) -> None:
        self.id = method_id
        self._events = events or []
        self.created: list[_ScriptedSource] = []

    def info(self) -> TraceMethodInfo:
        return TraceMethodInfo(
            id=self.id,
            label=self.id,
            description="",
            warning=None,
            live_state=True,
            setup_hint=None,
            settings_schema=_Settings.model_json_schema(),
        )

    def create_source(self, settings: BaseModel) -> _ScriptedSource:
        source = _ScriptedSource(list(self._events))
        self.created.append(source)
        return source


class _FailingMethod:
    """Always raises from `create_source` -- simulates a method that can't start."""

    settings_model: type[BaseModel] = _Settings

    def __init__(self, method_id: str = "failing") -> None:
        self.id = method_id

    def info(self) -> TraceMethodInfo:
        return TraceMethodInfo(
            id=self.id,
            label=self.id,
            description="",
            warning=None,
            live_state=True,
            setup_hint=None,
            settings_schema=_Settings.model_json_schema(),
        )

    def create_source(self, settings: BaseModel) -> _ScriptedSource:
        raise RuntimeError("simulated start failure")


@pytest.mark.asyncio
async def test_apply_config_starts_a_requested_method() -> None:
    spy = _SpyMethod("a")
    channel = RuntimeChannel(methods={"a": spy})

    statuses = await channel.apply_config([MethodConfig(id="a", settings=_Settings())])

    assert len(spy.created) == 1
    assert len(statuses) == 1
    assert statuses[0].method_id == "a"
    assert statuses[0].state == "running"


@pytest.mark.asyncio
async def test_apply_config_stops_a_method_no_longer_requested() -> None:
    spy = _SpyMethod("a")
    channel = RuntimeChannel(methods={"a": spy})
    await channel.apply_config([MethodConfig(id="a", settings=_Settings())])
    assert not spy.created[0].stopped

    statuses = await channel.apply_config([])  # empty list -- remove everything

    assert spy.created[0].stopped
    assert statuses == []


@pytest.mark.asyncio
async def test_apply_config_leaves_an_unchanged_method_running() -> None:
    """Same id, same settings (by value, not identity) -- `apply_config`
    must not tear down and recreate a source that didn't actually change."""
    spy = _SpyMethod("a")
    channel = RuntimeChannel(methods={"a": spy})
    await channel.apply_config([MethodConfig(id="a", settings=_Settings(tag="x"))])

    await channel.apply_config([MethodConfig(id="a", settings=_Settings(tag="x"))])

    assert len(spy.created) == 1  # create_source called only once
    assert not spy.created[0].stopped


@pytest.mark.asyncio
async def test_apply_config_restarts_a_method_whose_settings_changed() -> None:
    spy = _SpyMethod("a")
    channel = RuntimeChannel(methods={"a": spy})
    await channel.apply_config([MethodConfig(id="a", settings=_Settings(tag="x"))])
    first_source = spy.created[0]

    await channel.apply_config([MethodConfig(id="a", settings=_Settings(tag="y"))])

    assert len(spy.created) == 2
    assert first_source.stopped
    assert not spy.created[1].stopped


@pytest.mark.asyncio
async def test_a_failing_source_does_not_block_the_others_from_starting_and_reporting_events() -> None:
    """Task 22 Verification 6: with two methods configured, one that fails
    to start leaves the other running and actively reporting events."""
    good = _SpyMethod("good", events=[_event()])
    bad = _FailingMethod("bad")
    channel = RuntimeChannel(methods={"good": good, "bad": bad}, dead_letter_stats_interval_seconds=0.02)
    await channel.start()
    try:
        statuses = await channel.apply_config(
            [MethodConfig(id="good", settings=_Settings()), MethodConfig(id="bad", settings=_Settings())]
        )
        by_id = {s.method_id: s for s in statuses}
        assert by_id["good"].state == "running"
        assert by_id["bad"].state == "error"
        assert by_id["bad"].detail == "simulated start failure"
        assert len(good.created) == 1

        for _ in range(50):
            fresh = {s.method_id: s for s in await channel.source_statuses()}
            if fresh["good"].events_received >= 1:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("the surviving 'good' source never reported its event")
        assert fresh["good"].events_received == 1
        assert fresh["bad"].state == "error"  # still down, didn't recover on its own
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_apply_config_with_an_unknown_registry_id_marks_that_entry_as_error() -> None:
    """Defensive path: `apply_config` itself doesn't validate ids against
    the registry (the PUT handler already does, per Task 22's Design) --
    but calling it with one anyway must not crash or silently drop the
    entry; see `test_runtime_config.py` for the HTTP-level 422 this same
    case produces through the real endpoint."""
    channel = RuntimeChannel(methods={})

    statuses = await channel.apply_config([MethodConfig(id="nonexistent", settings=_Settings())])

    assert len(statuses) == 1
    assert statuses[0].method_id == "nonexistent"
    assert statuses[0].state == "error"
