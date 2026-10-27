"""`TraceMethod` Protocol + its wire-facing descriptor, `TraceMethodInfo`.

A method is a self-describing factory for one kind of `TraceSource`: it
knows its own settings shape (as a flat Pydantic model) and how to turn a
validated settings instance into a running `TraceSource`. The registry in
`runtime/methods/__init__.py` is the only place that lists which methods
exist — see that module's docstring for how to add one.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from agentic_visualizer.runtime.sources.base import TraceSource

# Same convention as `runtime/messages.py`'s `_RUNTIME_MSG_CONFIG` (a
# separate constant, not an import of that leading-underscore one, since
# this is wire shape for the HTTP method-registry endpoints, not the
# WebSocket message schema that module owns) -- every multi-word field on
# a `TraceMethodInfo` or a settings model goes camelCase on the wire, same
# as the rest of this app's JSON API.
METHOD_CONFIG = ConfigDict(frozen=True, alias_generator=to_camel, populate_by_name=True)


class TraceMethodInfo(BaseModel):
    """What `GET /runtime/methods` returns for one registered method --
    everything the Setup screen needs to render a row and a settings form
    without any method-specific frontend code (Task 22's whole point)."""

    model_config = METHOD_CONFIG

    id: str
    """Stable key, e.g. `"apv_live_file"`. Used as the dict key in
    `PUT /runtime/config`'s body and in `SourceStatus.method_id`."""

    label: str
    description: str

    warning: str | None
    """Shown in an amber box under this method's row whenever it's not
    None -- regardless of whether the method is currently checked. The
    point is to be read BEFORE turning the method on, not after."""

    live_state: bool
    """True when this method can show a node as `active` WHILE a call is
    still running (needs a `start` event with a matching `end`/`error`
    later). False for after-the-fact methods (Task 24) that only ever
    report completed calls."""

    setup_hint: str | None
    """Copyable text shown once the method is checked. `{field}`
    placeholders are filled from the method's own settings field values —
    e.g. `{path}` for `apv_live_file`'s single `path` setting."""

    settings_schema: dict[str, Any]
    """`settings_model.model_json_schema()`, verbatim. The frontend's
    `SchemaFields` renders flat `string`/`integer`/`boolean` properties
    only -- see `test_methods.py`'s schema-subset test, which every
    registered method's schema must pass."""


class TraceMethod(Protocol):
    """One pluggable way to get trace events into the app. See this
    package's `__init__.py` docstring for the three steps to add one."""

    id: str
    settings_model: type[BaseModel]

    def info(self) -> TraceMethodInfo:
        """Static-ish descriptor for `GET /runtime/methods`. Cheap to call
        repeatedly -- no I/O, just construct-and-return."""
        ...

    def create_source(self, settings: BaseModel) -> TraceSource:
        """Build a fresh, not-yet-started `TraceSource` from validated
        settings. Caller (`RuntimeChannel.apply_config`) always passes an
        instance of this method's own `settings_model` -- never call this
        with an arbitrary `BaseModel` by hand."""
        ...
