"""ResearchAgent — LangChain-flavored agent using @tool-decorated tools.

The Mongo `agents` record for this class references its tools by their
Python names (`web_search_tool`, `summarize_url_tool`, `calculator_tool`).
The scanner must:

  1. Recognize `ResearchAgent` as an agent node (via the Mongo record, not
     via any code marker — this class has no framework base class).
  2. Match the tool_ids in Mongo to the imported names here, then follow
     those imports back to `src/tools/langchain_tools.py`.
"""

from __future__ import annotations

from typing import Any

# NOTE: importing from the package re-exports on purpose — see tools/__init__.py.
from ..tools import web_search_tool, summarize_url_tool, calculator_tool
from ..services.llm_service import complete


class ResearchAgent:
    """Agent that answers research questions using web + summarization tools."""

    def __init__(self, model: str = "mock-model") -> None:
        self.model = model
        self.tools = [web_search_tool, summarize_url_tool, calculator_tool]

    async def run(self, question: str) -> str:
        """Entry point — called from the API layer."""
        prompt = self._build_prompt(question)
        return await complete(prompt, model=self.model)

    def _build_prompt(self, question: str) -> str:
        """Private helper. The scanner should still record calls to this method."""
        tool_list = ", ".join(getattr(t, "__name__", str(t)) for t in self.tools)
        return f"[{tool_list}] {question}"

    @staticmethod
    def describe() -> dict[str, Any]:
        """Static method — exercises the `@staticmethod` decorator path."""
        return {"kind": "research", "framework": "langchain"}
