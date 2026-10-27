"""Research and Write — mirrors mock_agent's `research_and_write` workflow.

Canonical path through a subset of the workflow's 7 tagged members (per this
task's Scope: "pick one canonical path and script it" — the full record also
includes Summarizer/Archive Curator/Content Moderator as standalone
workflow-members with no delegation edge FROM Coordinator, so they don't
belong in a single linear "run" narrative). Coordinator's own tools exclude
`missing_impl_tool`, a deliberate L2-resolution distractor in the seed data
that has nothing to do with runtime behavior.
"""

from __future__ import annotations

from apv_trace import trace

from . import simulate_agent

WORKFLOW_NAME = "Research and Write"

# Recording mode (video/gif): each tool step gets its own random duration in
# this range instead of a fixed per-agent total split evenly -- varied,
# visually readable pacing instead of a uniform tick. 10s is a hard cap so no
# single step ever drags a recording out.
_TOOL_DURATION_RANGE = (1.0, 5.0)


@trace(kind="workflow", name=WORKFLOW_NAME)
async def run(fail: bool = False) -> None:
    await simulate_agent(
        "Coordinator", tools=["internal_search", "file_read", "file_write"], tool_duration_range=_TOOL_DURATION_RANGE
    )
    await simulate_agent(
        "Research Agent", tools=["web_search", "summarize_url", "calculator"], tool_duration_range=_TOOL_DURATION_RANGE
    )
    await simulate_agent(
        "Writer Agent", tools=["document_query", "index_ingest", "citation_lookup"], tool_duration_range=_TOOL_DURATION_RANGE
    )
    await simulate_agent(
        "Code Reviewer", tools=["web_search", "file_read"], tool_duration_range=_TOOL_DURATION_RANGE, fail=fail
    )
