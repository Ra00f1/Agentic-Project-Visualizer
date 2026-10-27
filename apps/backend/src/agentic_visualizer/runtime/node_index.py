"""NodeIndex — resolves a `TraceEvent` to the graph node(s) it belongs to.

Seven strategies, tried in order (see `resolve`). Built once per `Graph`
(`NodeIndex.build`), swapped atomically under a lock by `RuntimeChannel`
whenever a fresh graph is available (see that module for why "fresh graph
available" means "a /graph call happened" in this codebase, not a
`RefreshCompleted` event — there's no persistent, subscribable graph state
here to hook one off of).

**Module-path derivation.** `TraceRef.module` is a Python dotted module path
(`fn.__module__`, e.g. `"runtime_demo.tools"`). The only place the graph
carries anything resembling code location is `function` nodes' `file_path`
attribute (from L2/L3), which is a POSIX-relative path from the codebase
root (e.g. `"runtime_demo/tools.py"`). `_module_from_file_path` converts one
to the other. This only produces the right answer when the codebase root
handed to the scanner IS the target project's actual Python import root
(package layout == scan root) — true for every fixture in this repo, and
the general case for a well-formed Python project.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentic_visualizer.domain import Edge, Graph, Node
from agentic_visualizer.trace.events import TraceEvent


@dataclass(frozen=True, slots=True)
class Resolved:
    """One or more graph nodes a `TraceEvent` was matched to, and how."""

    node_ids: list[str]
    strategy: str


@dataclass(frozen=True, slots=True)
class Unresolved:
    """No graph node could be matched. `reason` is human-readable, for the dead letter buffer."""

    reason: str


ResolveResult = Resolved | Unresolved


class NodeIndex:
    """Seven lookup dicts, one per resolution strategy, built once from a `Graph`."""

    def __init__(
        self,
        *,
        by_db_id: dict[str, str],
        by_framework_id: dict[str, str],
        by_module_qualname: dict[tuple[str, str, str], str],
        by_tool_fn: dict[tuple[str, str], str],
        fn_to_tools: dict[str, list[str]],
        by_typed_name: dict[tuple[str, str], list[str]],
        models_by_casefold_name: dict[str, list[str]],
    ) -> None:
        self.by_db_id = by_db_id
        self.by_framework_id = by_framework_id
        self.by_module_qualname = by_module_qualname
        self.by_tool_fn = by_tool_fn
        self.fn_to_tools = fn_to_tools
        self.by_typed_name = by_typed_name
        self.models_by_casefold_name = models_by_casefold_name

    @classmethod
    def build(cls, graph: Graph) -> NodeIndex:
        by_db_id: dict[str, str] = {}
        by_framework_id: dict[str, str] = {}
        by_module_qualname: dict[tuple[str, str, str], str] = {}
        by_tool_fn: dict[tuple[str, str], str] = {}
        by_typed_name: dict[tuple[str, str], list[str]] = {}
        models_by_casefold_name: dict[str, list[str]] = {}

        for node in graph.nodes:
            by_typed_name.setdefault((node.type, node.name), []).append(node.id)

            db_id = _db_id_of(node)
            if db_id is not None:
                by_db_id[db_id] = node.id

            framework_id = node.attributes.get("framework_id")
            if isinstance(framework_id, str) and framework_id:
                by_framework_id[framework_id] = node.id

            if node.type == "function":
                module = _module_from_file_path(node.attributes.get("file_path"))
                qualname = node.attributes.get("qualname")
                if module and isinstance(qualname, str) and qualname:
                    by_module_qualname[(node.type, module, qualname)] = node.id

            if node.type == "tool":
                fn_key = _tool_fn_key(node)
                if fn_key is not None:
                    by_tool_fn[fn_key] = node.id

            if node.type == "model":
                models_by_casefold_name.setdefault(node.name.casefold(), []).append(node.id)

        fn_to_tools = _build_fn_to_tools(graph.edges)

        return cls(
            by_db_id=by_db_id,
            by_framework_id=by_framework_id,
            by_module_qualname=by_module_qualname,
            by_tool_fn=by_tool_fn,
            fn_to_tools=fn_to_tools,
            by_typed_name=by_typed_name,
            models_by_casefold_name=models_by_casefold_name,
        )

    def resolve(self, event: TraceEvent) -> ResolveResult:
        ref = event.ref

        if ref.db_id and (nid := self.by_db_id.get(ref.db_id)):
            return Resolved([nid], strategy="db_id")

        if ref.framework_id and (nid := self.by_framework_id.get(ref.framework_id)):
            return Resolved([nid], strategy="framework_id")

        if ref.qualname and (nid := self.by_module_qualname.get((event.kind, ref.module, ref.qualname))):
            # Bridge: the function this event directly names, plus any tool
            # that implements it -- lights up the tool even though only the
            # underlying function was instrumented. `fn_to_tools` already
            # excludes functions with more than one implementing tool (see
            # _build_fn_to_tools) so this never bridges to an unrelated tool.
            #
            # Task 18's `--no-bridge`: a function already reported as a
            # tool/agent span by something else (an OpenInference
            # instrumentor, or a raw `apv.kind=tool` span opened inside the
            # function body) would otherwise count two runs per call once
            # `apv-run --functions` ALSO reports the same call as a plain
            # function event. `meta.bridge is False` is the function
            # tracer's own escape hatch for that -- skip the extension and
            # report the plain strategy instead.
            if event.meta.get("bridge") is False:
                return Resolved([nid], strategy="module_qualname")
            node_ids = [nid, *self.fn_to_tools.get(nid, [])]
            return Resolved(node_ids, strategy="module_qualname+bridge")

        if event.kind == "tool" and (nid := self.by_tool_fn.get((ref.name, ref.module))):
            return Resolved([nid], strategy="tool_fn")

        matches = self.by_typed_name.get((event.kind, ref.name), [])
        if len(matches) == 1:
            return Resolved(matches, strategy="typed_name")
        if len(matches) > 1:
            return Unresolved(f"ambiguous name: {len(matches)} candidates for ({event.kind}, {ref.name})")

        if event.kind == "model":
            prefix_result = self._resolve_model_prefix(ref.name)
            if prefix_result is not None:
                return prefix_result

        return Unresolved(f"no node matches ref={ref!r} kind={event.kind}")

    def _resolve_model_prefix(self, ref_name: str) -> ResolveResult | None:
        """Match a dated model id (`claude-sonnet-4-20250514`) to the short
        DB name (`claude-sonnet-4`) it's a dated snapshot of -- tried only
        after an exact `typed_name` match has already failed (see `resolve`).

        A candidate's casefolded name must equal `ref_name`'s, or be a
        prefix of it immediately followed by `-` -- that dash boundary is
        what stops `claude-sonnet-4` from matching `claude-sonnet-45`
        (a different, unrelated model). Among all matching candidates, the
        LONGEST name wins (`gpt-4o-mini` over `gpt-4o` for an id starting
        with "gpt-4o-mini-"), since a longer name is the more specific
        match. `None` means no candidate matched at all -- the caller
        falls through to the generic "no node matches" `Unresolved`, not a
        model-specific one, so dead letters stay easy to tell apart by
        their own message.
        """
        target = ref_name.casefold()
        best_name: str | None = None
        for candidate_name in self.models_by_casefold_name:
            is_match = candidate_name == target or target.startswith(candidate_name + "-")
            if is_match and (best_name is None or len(candidate_name) > len(best_name)):
                best_name = candidate_name
        if best_name is None:
            return None

        node_ids = self.models_by_casefold_name[best_name]
        if len(node_ids) > 1:
            return Unresolved(f"ambiguous model prefix: {len(node_ids)} nodes named {best_name!r}")
        return Resolved(node_ids, strategy="model_prefix")


def _db_id_of(node: Node) -> str | None:
    """The raw DB-native id a target project's `TraceRef.db_id` would carry
    for this node.

    Prefers `node.attributes["db_id"]`, set explicitly by whichever scanner
    produced the node (every connector can set this -- it's not Mongo-
    specific) -- see `scanners.l1_entity`'s `Node(...)` construction for the
    Mongo case. Falls back to parsing it out of `Node.id` ONLY when that
    attribute is absent: `Node.id` format for mongo-sourced nodes is
    `mongo:<db>.<collection>:<oid>` (see `domain/node.py`), and the trailing
    segment IS that same id. This fallback exists so fixtures/graphs built
    before scanners wrote `attributes["db_id"]` explicitly keep resolving
    without a re-scan; new connectors should rely on the attribute, not this
    parse.
    """
    db_id = node.attributes.get("db_id")
    if isinstance(db_id, str) and db_id:
        return db_id
    if not node.id.startswith("mongo:"):
        return None
    _, _, oid = node.id.rpartition(":")
    return oid or None


def _module_from_file_path(file_path: object) -> str | None:
    if not isinstance(file_path, str) or not file_path:
        return None
    without_suffix = file_path[:-3] if file_path.endswith(".py") else file_path
    return without_suffix.replace("/", ".")


def _tool_fn_key(tool_node: Node) -> tuple[str, str] | None:
    """`(function_name, module_path)` for a tool node, matching L2's own extraction PRIORITY.

    Mirrors `scanners.l2_tool._extract_import_path`, including which source
    wins when a tool record has both: `import_path` first (the pre-composed
    dotted path L2 actually resolves against), falling back to the separate
    `function_name`/`module_path` attributes only when `import_path` is
    absent. Getting this order backwards would compute a different key than
    the one L2 used to resolve/verify the function whenever a tool record's
    two representations disagree (e.g. a bound method where `import_path`
    includes the class segment but a separately-stored `module_path`
    doesn't) -- silently failing to resolve events that should match.
    Deliberately duplicated rather than imported -- importing scanner
    internals into the runtime package would be a one-off coupling for a
    handful of lines.
    """
    attrs = tool_node.attributes
    import_path = attrs.get("import_path")
    if isinstance(import_path, str) and "." in import_path:
        module_path, _, function_name = import_path.rpartition(".")
        return (function_name, module_path)

    fallback_function_name = attrs.get("function_name")
    fallback_module_path = attrs.get("module_path")
    if (
        isinstance(fallback_function_name, str)
        and fallback_function_name
        and isinstance(fallback_module_path, str)
        and fallback_module_path
    ):
        return (fallback_function_name, fallback_module_path)

    return None


def _build_fn_to_tools(edges: list[Edge]) -> dict[str, list[str]]:
    """function_node_id -> [tool_node_ids], via `implements` edges.

    Only kept when exactly one tool implements a given function (Task 3's
    own Risk note): a function called from many unrelated places, wrapped
    by just one tool, should still make that tool glow. A function wrapped
    by *multiple* tools has no single right tool to light up, so bridging
    is suppressed for it entirely -- only the function itself glows.
    """
    all_implementers: dict[str, list[str]] = {}
    for edge in edges:
        if edge.kind == "implements":
            all_implementers.setdefault(edge.target_id, []).append(edge.source_id)
    return {fn_id: tools for fn_id, tools in all_implementers.items() if len(tools) == 1}
