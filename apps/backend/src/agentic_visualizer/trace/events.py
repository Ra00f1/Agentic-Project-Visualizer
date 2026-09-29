"""TraceEvent — one line of a target project's runtime-trace log.

Design decisions worth naming here so they don't drift:

* **No camelCase aliasing, unlike `domain.node`/`domain.edge`.** Those models
  get `alias_generator=to_camel` because they're served straight to the TS
  frontend. `TraceEvent` is different: it's the on-disk JSONL contract
  between a target project's `apv_trace` emitter and this app's file-tailing
  `TraceSource`, and that emitter writes plain snake_case keys (`run_id`,
  `span_id`, `parent_span_id`, ...) without going through Pydantic at all —
  see `runtime-demo-project/apv_trace/__init__.py`. Aliasing this model would
  make `TraceEvent.model_validate_json()` reject the exact lines the emitter
  writes. The WebSocket-facing runtime messages (task NN+2) are a separate,
  camelCase model that wraps data derived from these events — this schema
  only has to agree with the log file.

* **`meta` stays an open dict, `TraceError` isn't nested inside it.** Mirrors
  `Node.attributes` in `domain.node`: emitters attach an `"error"` key shaped
  like `TraceError` on the `error` phase, but `meta` isn't restricted to that
  so future `kind`s can carry arbitrary diagnostic data without a schema
  migration. `TraceError` is still a first-class model — mainly so it has one
  place to document its fields and can be validated explicitly by anything
  that wants to (e.g. the frontend detail panel's TS mirror, task NN+3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Every value type here is immutable, same rationale as `domain.node`: a
# trace event is a fact about something that already happened, and mutating
# one in place would be a bug waiting to happen in the correlation/state
# pipeline (task NN+2) that consumes a stream of these.
_TRACE_CONFIG = ConfigDict(frozen=True)


class TraceRef(BaseModel):
    """Identifies *what* emitted a trace event, for correlation against the graph.

    Resolution (task NN+2) tries these in order: `db_id`, `framework_id`,
    `(module, qualname)`, then falls back to `(kind, name)` typed-name
    matching. A target project's `apv_trace` decorator only ever fills in
    `name`/`module`/`qualname` from the function it wraps — `framework_id`
    and `db_id` are there for future framework adapters and DB-seeded
    projects to populate, not required for a trace event to be valid.
    """

    model_config = _TRACE_CONFIG

    name: str = Field(min_length=1)
    module: str = Field(min_length=1)
    qualname: str | None = None
    framework_id: str | None = None
    db_id: str | None = None


class TraceError(BaseModel):
    """Shape of the `meta["error"]` value an emitter attaches on `phase="error"`.

    `traceback` is truncated by the emitter (4KB default) before it's ever
    written — this model doesn't re-enforce that, it just documents what to
    expect.
    """

    model_config = _TRACE_CONFIG

    type: str = Field(min_length=1)
    message: str
    traceback: str


class TraceEvent(BaseModel):
    """One lifecycle boundary (start/end/error) of one traced call.

    `v` is a schema version tag, not a Pydantic model version — bump it (and
    add a new model) if this shape ever changes incompatibly, so an old
    tailer reading a new log format fails loudly instead of silently
    misparsing.
    """

    model_config = _TRACE_CONFIG

    v: Literal[1] = 1
    ts: datetime
    run_id: str = Field(min_length=1)
    span_id: str = Field(min_length=1)
    parent_span_id: str | None = None
    phase: Literal["start", "end", "error"]
    kind: Literal["workflow", "agent", "tool", "function"]
    ref: TraceRef
    meta: dict[str, Any] = Field(default_factory=dict)
