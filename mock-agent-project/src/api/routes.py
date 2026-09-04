"""FastAPI routes.

Every endpoint form we expect to meet:
  * `@router.get` and `@router.post` (decorator = attribute call).
  * A path parameter (`/documents/{doc_id}`).
  * A `Depends(...)` injected argument.
  * A response_model kwarg.
  * A trailing slash + non-trailing slash pair (real projects have both).

Each handler also drives the L3 call chain: `list_documents` →
`services.search_service.search` → `repositories.document_repo.default_repo.fetch_all`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..models.schemas import (
    DocumentSummary,
    ListDocumentsResponse,
    RunAgentRequest,
    RunAgentResponse,
)
from ..services import search_service
from ..agents.coordinator_agent import CoordinatorAgent


router = APIRouter(prefix="/api", tags=["mock"])


def get_coordinator() -> CoordinatorAgent:
    """FastAPI dependency — returns a fresh coordinator per request."""
    return CoordinatorAgent()


@router.get("/documents", response_model=ListDocumentsResponse)
def list_documents(limit: int = 25) -> ListDocumentsResponse:
    """Top of the canonical L3 chain: handler → service → repo."""
    docs = search_service.search(query="", limit=limit)
    items = [
        DocumentSummary(id=d.get("_id", ""), title=d.get("title", ""), tags=d.get("tags", []))
        for d in docs
    ]
    return ListDocumentsResponse(items=items, total=len(items))


@router.get("/documents/{doc_id}", response_model=DocumentSummary)
def get_document(doc_id: str) -> DocumentSummary:
    """Path-parameter endpoint."""
    from ..repositories.document_repo import default_repo  # in-function import — deliberate

    doc = default_repo.fetch_by_id(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="not found")
    return DocumentSummary(id=doc["_id"], title=doc["title"], tags=doc.get("tags", []))


@router.post("/agents/run", response_model=RunAgentResponse)
async def run_agent(
    body: RunAgentRequest,
    coordinator: CoordinatorAgent = Depends(get_coordinator),
) -> RunAgentResponse:
    """POST with request body + Depends injection."""
    output = await coordinator.handle(body.input)
    return RunAgentResponse(agent_id=body.agent_id, output=output, trace_id=None)


@router.get("/documents/by-tag/{tag}")
def documents_by_tag(tag: str) -> list[dict]:
    """Second endpoint that traverses the same call chain via a different service fn."""
    return search_service.search_by_tag(tag)
