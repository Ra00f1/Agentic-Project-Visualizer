"""Pydantic request/response schemas used by the API layer.

These are here so the L4 endpoint mapper has typed request/response models to
attribute to each endpoint node. In v1 we don't render them as separate graph
nodes — but we do surface them in the endpoint's detail panel, and their
presence should NOT create phantom function nodes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DocumentSummary(BaseModel):
    id: str
    title: str
    tags: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class ListDocumentsResponse(BaseModel):
    items: list[DocumentSummary]
    total: int


class RunAgentRequest(BaseModel):
    agent_id: str
    input: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunAgentResponse(BaseModel):
    agent_id: str
    output: str
    trace_id: str | None = None
