"""Agent registry — re-exports every agent class by name."""

from .research_agent import ResearchAgent
from .writer_agent import WriterAgent
from .coordinator_agent import CoordinatorAgent

__all__ = ["ResearchAgent", "WriterAgent", "CoordinatorAgent"]
