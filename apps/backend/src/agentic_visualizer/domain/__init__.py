"""Domain primitives.

The two things every feature in this app reads and writes: `Node` and `Edge`.
Framework-specific shapes (a LangChain AgentExecutor, a Mongo document, an AST
node) never leak into this module. If a new scanner wants to add a concept, it
either fits into `attributes` on an existing node type or gets a new
`NodeType`/`EdgeKind` value here.
"""

from .node import Node, NodeType, Provenance
from .edge import Edge, EdgeKind
from .graph import Graph, ScanError, ScanResult

__all__ = [
    "Node",
    "NodeType",
    "Provenance",
    "Edge",
    "EdgeKind",
    "Graph",
    "ScanError",
    "ScanResult",
]
