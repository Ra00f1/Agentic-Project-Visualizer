"""WriterAgent — LlamaIndex-flavored agent.

Its tools are FunctionTool wrappers over private functions. The Mongo record
for this agent references the tools by their WRAPPED-function names
(`_document_query`, `_index_ingest`, `_citation_lookup`), not the
FunctionTool variable names. The scanner's LlamaIndex adapter must resolve
either identifier back to the same code node.
"""

from __future__ import annotations

from ..tools.llamaindex_tools import (
    document_query_tool,
    index_ingest_tool,
    citation_lookup_tool,
)
from ..services.llm_service import complete_many


class WriterAgent:
    """Agent that drafts long-form content grounded in an indexed corpus."""

    def __init__(self, model: str = "mock-model", index_name: str = "default") -> None:
        self.model = model
        self.index_name = index_name
        self.tools = [document_query_tool, index_ingest_tool, citation_lookup_tool]

    async def draft(self, outline: list[str]) -> list[str]:
        """Draft several sections concurrently — exercises TaskGroup fan-out."""
        prompts = [f"Write section: {o}" for o in outline]
        return await complete_many(prompts, model=self.model)
