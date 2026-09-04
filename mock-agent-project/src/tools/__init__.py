"""Tool registry — re-exports every tool by name.

The re-export pattern is deliberate: the scanner should follow the alias from
`src.tools.web_search_tool` back to `src.tools.langchain_tools.web_search_tool`.
If we ever silently drop that hop, tool→function resolution will regress.
"""

from .langchain_tools import (
    web_search_tool,
    summarize_url_tool,
    calculator_tool,
)
from .llamaindex_tools import (
    document_query_tool,
    index_ingest_tool,
    citation_lookup_tool,
)
from .custom_tools import (
    internal_search_tool,
    file_read_tool,
    file_write_tool,
)

__all__ = [
    "web_search_tool",
    "summarize_url_tool",
    "calculator_tool",
    "document_query_tool",
    "index_ingest_tool",
    "citation_lookup_tool",
    "internal_search_tool",
    "file_read_tool",
    "file_write_tool",
]
