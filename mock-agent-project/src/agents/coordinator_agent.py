"""CoordinatorAgent — custom-framework agent.

Exercises:
  * Aliased imports (`from ..tools import custom_tools as ct`).
  * Delegation to other agents (edges from agent → agent, not just agent → tool).
  * `@timed` wrapper via functools.wraps.
"""

from __future__ import annotations

from ..tools import custom_tools as ct  # aliased import — deliberate
from ..utils.decorators import timed
from .research_agent import ResearchAgent
from .writer_agent import WriterAgent


class CoordinatorAgent:
    """Top-level orchestrator agent. Delegates to research + writer agents."""

    def __init__(self) -> None:
        self.research = ResearchAgent()
        self.writer = WriterAgent()
        self.tools = [ct.internal_search_tool, ct.file_read_tool, ct.file_write_tool]

    @timed  # functools.wraps wrapper — scanner should attribute calls to `handle`
    async def handle(self, task_input: str) -> str:
        """Dispatch a task to the right sub-agent based on a trivial rule."""
        if task_input.lower().startswith("draft"):
            sections = await self.writer.draft([task_input])
            return "\n".join(sections)
        return await self.research.run(task_input)

    def save_result(self, path: str, result: str) -> bool:
        """Persist a result via the file_write custom tool."""
        return ct.file_write_tool(path, result, overwrite=True)
