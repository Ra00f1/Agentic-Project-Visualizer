"""Edge — a directed relationship between two nodes.

An edge is uniquely identified by the triple `(source_id, target_id, kind)`.
We deliberately do NOT give edges their own IDs: it forces callers to think
about deduplication (two different scanner runs must produce the same edge
for the same relationship, otherwise the merge story breaks), and it keeps
the wire format smaller.

The `kind` vocabulary maps 1:1 to pipeline stages so the UI legend stays stable:

    uses          — L1 (agent uses tool, workflow uses agent, agent uses model)
    owns          — L1 (user owns file, user owns prompt)
    contains      — L1 (workflow contains file)
    delegates_to  — L1 (agent delegates to sub-agent)
    implements    — L2 (tool implements function)
    calls         — L3 (function calls function)
    exposes       — L4 (endpoint exposes function)

Later stages are declared now so the palette/legend can be built once and
never revisited when L2/L3/L4 land.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .node import Provenance, _DOMAIN_CONFIG

EdgeKind = Literal[
    "uses",
    "owns",
    "contains",
    "delegates_to",
    "implements",  # reserved for L2
    "calls",       # reserved for L3
    "exposes",     # reserved for L4
]


class Edge(BaseModel):
    """A directed edge from `source_id` to `target_id`.

    Both endpoints must resolve to real `Node.id`s in the same `Graph`.
    Validation of that invariant lives on `Graph`, not here — a scanner is
    allowed to emit an edge whose target hasn't been created yet, and the
    graph assembly step surfaces the mismatch as a `ScanError`. Keeping edges
    dumb keeps scanners simple.
    """

    model_config = _DOMAIN_CONFIG

    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    kind: EdgeKind
    provenance: Provenance
    attributes: dict[str, Any] = Field(default_factory=dict)
