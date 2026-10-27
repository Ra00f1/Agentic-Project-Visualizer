"""`OtlpHttpMethod` -- a loopback OTLP/HTTP receiver, for a target project
that already speaks OpenTelemetry (Task 23). See `runtime/sources/otlp_http.py`
for the receiver itself; this module is just the `TraceMethod` wrapper the
Setup screen's registry (`runtime/methods/__init__.py`) needs.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, Field

from agentic_visualizer.runtime.methods.base import METHOD_CONFIG, TraceMethodInfo
from agentic_visualizer.runtime.sources.base import TraceSource
from agentic_visualizer.runtime.sources.otlp_http import OtlpHttpTraceSource

_DEFAULT_PORT = 4318
"""The OTLP/HTTP standard's own default port -- matches a target's own
exporter default when `OTEL_EXPORTER_OTLP_ENDPOINT` just overrides the host."""


class OtlpHttpSettings(BaseModel):
    """The one setting this method needs: which loopback port to listen on."""

    model_config = METHOD_CONFIG

    port: int = Field(
        default=_DEFAULT_PORT,
        ge=1024,
        le=65535,
        title="Receiver port",
        description="Loopback port the OTLP/HTTP receiver listens on (127.0.0.1 only).",
    )


class OtlpHttpMethod:
    """`id="otlp_http"` -- see `runtime/methods/__init__.py` for the registry."""

    id = "otlp_http"
    settings_model: type[BaseModel] = OtlpHttpSettings

    def info(self) -> TraceMethodInfo:
        return TraceMethodInfo(
            id=self.id,
            label="OpenTelemetry (OTLP)",
            description=(
                "No code changes for programs that already use OpenTelemetry. Shows each "
                "call shortly after it finishes, not while it runs."
            ),
            warning=None,
            live_state=False,
            setup_hint=(
                '$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://127.0.0.1:{port}"\n'
                '$env:OTEL_EXPORTER_OTLP_PROTOCOL = "http/protobuf"\n'
                '$env:OTEL_BSP_SCHEDULE_DELAY = "500"'
            ),
            settings_schema=OtlpHttpSettings.model_json_schema(),
        )

    def create_source(self, settings: BaseModel) -> TraceSource:
        # Same invariant as `ApvLiveFileMethod.create_source`: the caller
        # (`RuntimeChannel.apply_config`) always validates through THIS
        # method's own `settings_model` first.
        otlp_settings = cast(OtlpHttpSettings, settings)
        return OtlpHttpTraceSource(otlp_settings.port)
