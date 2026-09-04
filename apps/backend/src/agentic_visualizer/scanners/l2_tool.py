"""L2: tool -> code function resolution.

Consumes L1's tool nodes and, for each one, tries to locate the concrete
Python function that implements it. On success: emits a `function` node and
an `implements` edge (tool -> function). On failure: emits a `ScanError`
scoped to the offending tool node's id, so the frontend can render an
unresolved-code warning against that exact tool.

Why "tool-driven" (walk the L1 output) rather than "code-driven" (walk the
codebase enumerating functions):

  * The authoritative signal for "this function is a tool" comes from
    Mongo, not from decorators. `custom_tools.py` has no decorator on
    `internal_search_tool` — Mongo is the only reason it counts as a tool.
    A code-driven scan would miss it. Meanwhile
    `utils/decorators.py:looks_like_a_tool_but_isnt` has a tool-shaped
    signature but no Mongo row, and MUST NOT appear as a tool. Only the
    tool-driven scan gets both right.
  * L3 will walk the codebase — L2 doesn't need to duplicate that work.

Deterministic output for the same inputs. Uses `context.now` for provenance
timestamps (never `datetime.now()` directly) so tests can assert on IDs
and timestamps stably.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..codebase.base import CodebaseAdapter, SymbolLocation
from ..domain import Edge, Node, Provenance, ScanError, ScanResult
from ..domain.node import NodeType
from .base import ScanContext


@dataclass(slots=True, frozen=True)
class L2ToolCodeScanner:
    """Resolve every tool node in `upstream` to a code function.

    `upstream` is the ScanResult from a prior L1 pass. We take its nodes
    and read only those whose `type == "tool"`; every other node is
    passed through unchanged. That preserves the "later stages consume
    upstream output, never mutate it" invariant in CLAUDE.md §6 — L2's
    output is *additive* (new function nodes + implements edges + warnings)
    and never re-emits L1's own nodes or edges.

    `codebase` is any `CodebaseAdapter`. In v1 that's always the local
    filesystem adapter; the GitHub-shallow-clone adapter slots in later
    with zero changes to L2 itself.
    """

    codebase: CodebaseAdapter
    upstream: ScanResult

    name: str = "l2_tool"

    async def scan(self, context: ScanContext) -> ScanResult:
        """Return only the *additive* delta: new function nodes,
        implements edges, and errors. Never re-emits upstream's own
        content. Callers merge both results via `Graph.from_results`.
        """
        # `now()` is called once per scan — one timestamp value shared by
        # every node and edge this scan emits. Matches L1's convention;
        # keeps provenance sortable across scans without hair-splitting
        # sub-second differences.
        scanned_at = context.now()

        nodes: list[Node] = []
        edges: list[Edge] = []
        errors: list[ScanError] = []
        # Dedup: two tool records could theoretically resolve to the same
        # function (e.g. two aliases for the same underlying code). The
        # merger dedups by node id anyway, but re-emitting is wasteful
        # so we short-circuit here too.
        seen_function_ids: set[str] = set()

        for tool_node in _iter_tools(self.upstream.nodes):
            import_path = _extract_import_path(tool_node)
            if not import_path:
                errors.append(
                    ScanError(
                        scanner=self.name,
                        source_ref=tool_node.id,
                        message=(
                            f"tool '{tool_node.name}' has no import_path attribute; "
                            "cannot resolve to code"
                        ),
                    )
                )
                continue

            # Attempt 1: direct dotted-path resolution. Fast — one file
            # read + parse when the tool's Mongo record is accurate.
            loc: SymbolLocation | None = None
            direct_failure: str | None = None
            try:
                loc = self.codebase.resolve_symbol(import_path)
                if loc is None:
                    direct_failure = (
                        f"module was found but no matching function "
                        f"definition inside it"
                    )
            except FileNotFoundError as exc:
                direct_failure = f"module not found: {exc}"

            # Attempt 2: name-based fallback across the whole codebase.
            # Fires only when the direct lookup failed — most projects
            # never pay for this scan. When it fires, cost is one AST
            # parse per .py file (cached on the adapter, so repeated
            # fallbacks share the cost within a single L2 scan).
            #
            # Decouples L2 from the "tools live in a folder named
            # tools/" convention: if the Mongo record's module_path is
            # stale but the function itself still exists under some
            # other folder / file name, we still find it.
            if loc is None:
                symbol_name = _extract_symbol_name(tool_node, import_path)
                if symbol_name:
                    candidates = self.codebase.resolve_symbol_by_name(symbol_name)
                    if len(candidates) == 1:
                        loc = candidates[0]
                    elif len(candidates) > 1:
                        # Ambiguous — refuse to guess. List up to 5 so
                        # the operator can pick which one to point the
                        # Mongo record at.
                        sample = ", ".join(
                            f"{c.file}:{c.line}" for c in candidates[:5]
                        )
                        more = "" if len(candidates) <= 5 else f" (+{len(candidates) - 5} more)"
                        errors.append(
                            ScanError(
                                scanner=self.name,
                                source_ref=tool_node.id,
                                message=(
                                    f"tool '{tool_node.name}' unresolved: "
                                    f"import_path '{import_path}' failed "
                                    f"({direct_failure}); and {len(candidates)} "
                                    f"top-level functions named "
                                    f"'{symbol_name}' exist under the codebase "
                                    f"root — please tighten the Mongo record's "
                                    f"module_path. Candidates: {sample}{more}"
                                ),
                            )
                        )
                        continue

            if loc is None:
                # Zero direct match AND zero fallback match — genuinely
                # unresolved. Message reflects both attempts so operators
                # know we tried the fallback too.
                errors.append(
                    ScanError(
                        scanner=self.name,
                        source_ref=tool_node.id,
                        message=(
                            f"tool '{tool_node.name}' references "
                            f"'{import_path}' — {direct_failure or 'unresolved'}, "
                            "and no top-level function with a matching name "
                            "was found anywhere under the codebase root"
                        ),
                    )
                )
                continue

            function_id = _function_node_id(loc)
            if function_id not in seen_function_ids:
                nodes.append(
                    Node(
                        id=function_id,
                        type="function",
                        name=loc.qualname,
                        provenance=Provenance(
                            source=self.name,
                            source_ref=f"{loc.file}:{loc.line}",
                            scanned_at=scanned_at,
                        ),
                        attributes={
                            # Snake_case keys — these pass through unchanged
                            # to the wire (see App.tsx GOTCHA about
                            # alias_generator not recursing into dict).
                            "file_path": loc.file,
                            "line": loc.line,
                            "qualname": loc.qualname,
                            "is_async": loc.is_async,
                            "decorators": list(loc.decorators),
                            "docstring": loc.docstring,
                            "resolution_kind": loc.kind,
                        },
                    )
                )
                seen_function_ids.add(function_id)

            edges.append(
                Edge(
                    source_id=tool_node.id,
                    target_id=function_id,
                    kind="implements",
                    provenance=Provenance(
                        source=self.name,
                        source_ref=tool_node.id,
                        scanned_at=scanned_at,
                    ),
                    attributes={
                        # Names how we found it — useful for the detail
                        # panel to explain "this tool's Mongo record points
                        # at a wrapper; the code lives at the underlying
                        # function." Also useful if we ever tighten the
                        # LlamaIndex heuristic and want to A/B compare.
                        "resolution_kind": loc.kind,
                    },
                )
            )

        return ScanResult(scanner=self.name, nodes=nodes, edges=edges, errors=errors)


# ---------------------------------------------------------------------------


def _iter_tools(nodes: Iterable[Node]) -> Iterable[Node]:
    """Yield only tool nodes from an arbitrary node iterable."""
    for n in nodes:
        if n.type == "tool":
            yield n


def _extract_import_path(tool_node: Node) -> str | None:
    """Prefer the pre-composed `import_path`; fall back to
    `module_path` + `function_name` when the DB only carries the pair.

    Real projects vary in what they store on their tool records — some
    have `import_path` explicitly (fixture case), some only the pair.
    Handling both keeps the scanner useful without a schema mapping
    round-trip.
    """
    attrs = tool_node.attributes
    import_path = attrs.get("import_path")
    if isinstance(import_path, str) and import_path:
        return import_path
    module_path = attrs.get("module_path")
    function_name = attrs.get("function_name")
    if isinstance(module_path, str) and isinstance(function_name, str):
        return f"{module_path}.{function_name}"
    return None


def _extract_symbol_name(tool_node: Node, import_path: str | None) -> str:
    """Best-effort symbol name for the name-based fallback lookup.

    Preference order matches how tightly the source ties the name to
    "this specific function definition":

      1. `function_name` attribute — the Mongo record's own field, most
         authoritative.
      2. Last dotted segment of `import_path` — derived, less
         authoritative but usually correct.

    Returns "" when neither is available — the caller skips fallback in
    that case.
    """
    attrs = tool_node.attributes
    fn = attrs.get("function_name")
    if isinstance(fn, str) and fn:
        return fn
    if import_path:
        return import_path.rpartition(".")[2]
    return ""


def _function_node_id(loc: SymbolLocation) -> str:
    """Deterministic function node id.

    Format matches CLAUDE.md's `code:<file>:<line>:<qualname>` convention.
    File path is already POSIX (adapter guarantees), so IDs read identical
    on Windows and Mac — the merger's `(source, source_ref)` identity
    stays stable across contributor machines.
    """
    return f"code:{loc.file}:{loc.line}:{loc.qualname}"


# `NodeType` re-export for callers that want to reference this scanner's
# output types explicitly. Cheap; keeps the domain import out of scanner
# consumer code.
FUNCTION_NODE_TYPE: NodeType = "function"
