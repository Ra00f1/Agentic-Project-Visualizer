"""`ApvLiveFileMethod` — tails the JSONL file the `apv_trace` helper writes.

The first (and, until Task 24, only) registered `TraceMethod`. Wraps the
existing `FileTailTraceSource` (Task 2/21) behind the method interface so
the Setup screen can offer it without any file-tailer-specific frontend
code.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import platformdirs
from pydantic import BaseModel, Field

from agentic_visualizer.runtime.methods.base import METHOD_CONFIG, TraceMethodInfo
from agentic_visualizer.runtime.sources.base import TraceSource
from agentic_visualizer.runtime.sources.file_tail import FileTailTraceSource


def _default_path() -> str:
    # Matches `apv_trace.default_log_path()`'s own formula exactly (Task
    # 21's D1 -- one shared, stable trace file per machine) WITHOUT
    # depending on the `apv_trace` package itself: CLAUDE.md §12 is
    # explicit that `apv_trace` ships with target projects only, never as
    # a runtime dependency of this app. `platformdirs` alone (flagged and
    # approved as a new backend dependency for this task) is enough to
    # reproduce the same path.
    return str(Path(platformdirs.user_log_dir("apv")) / "trace" / "live.jsonl")


class ApvLiveFileSettings(BaseModel):
    """The one setting this method needs: which file to tail."""

    model_config = METHOD_CONFIG

    # Deliberately `default=_default_path()` (called once, at import time),
    # NOT `default_factory=_default_path`: Pydantic v2's `model_json_schema()`
    # omits `default` entirely for a `default_factory` field (it won't call
    # an arbitrary factory just to produce a schema) -- which would leave
    # the Setup screen with no default to pre-fill or to fill the
    # `{path}` placeholder in `setup_hint` with. A plain eager default is
    # correct here anyway: `_default_path()` is a pure, side-effect-free
    # computation (platformdirs.user_log_dir(...) plus string joins) that
    # returns the same value every time within one process, so computing
    # it once at class-definition time loses nothing a factory would add.
    path: str = Field(
        default=_default_path(),
        title="Trace log path",
        description="The JSONL file your traced program's apv_trace.configure() (or apv-run) writes to.",
    )


class ApvLiveFileMethod:
    """`id="apv_live_file"` -- see this package's `__init__.py` for the
    registry it's listed in."""

    id = "apv_live_file"
    settings_model: type[BaseModel] = ApvLiveFileSettings

    def info(self) -> TraceMethodInfo:
        return TraceMethodInfo(
            id=self.id,
            label="Live trace (apv_trace)",
            description=(
                "Glows nodes while calls are running. Reads the JSONL file the "
                "apv_trace helper writes inside your program."
            ),
            warning=(
                "Needs the apv_trace helper running inside the program you trace: "
                "add apv-trace to its dependencies and call apv_trace.configure(), "
                "or start it with apv-run. Both sides must use the same file path."
            ),
            live_state=True,
            setup_hint='$env:APV_TRACE_LOG_PATH = "{path}"\napv-run -- python your_app.py',
            settings_schema=ApvLiveFileSettings.model_json_schema(),
        )

    def create_source(self, settings: BaseModel) -> TraceSource:
        # `settings` is typed as the Protocol's generic `BaseModel` (every
        # `TraceMethod.create_source` must accept that), but the caller
        # (`RuntimeChannel.apply_config`) always validates through THIS
        # method's own `settings_model` first -- `cast`, not a runtime
        # `isinstance` check, documents that invariant without re-checking
        # something the caller already guarantees.
        apv_settings = cast(ApvLiveFileSettings, settings)
        return FileTailTraceSource(Path(apv_settings.path))
