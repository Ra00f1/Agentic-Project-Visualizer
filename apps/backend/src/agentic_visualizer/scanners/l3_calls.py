"""L3: intra-project call graph.

Walks every Python file under the codebase root and, for each top-level
function definition, emits `calls` edges to the functions it invokes.
Cross-file resolution goes through the same `CodebaseAdapter` that L2
uses (so its AST cache is shared — files parsed by L2 are free here).

What L3 resolves in v1:

  * Bare-name calls to top-level defs in the same file:
      def foo(): bar()   # bar defined at module top level below
  * Bare-name calls to imported names:
      from .shared import normalize_query
      def x(): normalize_query(q)
  * Module-attribute calls where the module was imported as an alias:
      from ..services import search_service
      def x(): search_service.search(q)
  * Instance-method calls on module-level class singletons:
      default_repo = DocumentRepository()
      def x(): default_repo.fetch_all()   # -> DocumentRepository.fetch_all
  * Aliased imports (`import x as y`, `from x import y as z`) — the alias
    is what the caller uses, but resolution follows the underlying symbol.
  * Async functions — bodies walked the same as sync ones, `await f()`
    unwraps to the inner Call.

What v1 SKIPS (silently — no ScanError; these are the expected shape of
"we can't statically prove it" not "the code is broken"):

  * `self.attr()` calls (requires cross-method state tracking).
  * Chained calls (`f().g()` — target of .g() is a return value).
  * Dynamic dispatch (`getattr(x, "y")()`, `globals()["y"]()`).
  * Calls to stdlib / third-party (anything whose dotted path doesn't
    resolve inside the codebase root).
  * Class constructors (`Foo()` treated as a Foo call — target is not a
    function). Only method calls on tracked instances count.

Fan-in is preserved: two callers of `normalize_query` produce two edges
with the same `target_id` (`code:<file>:<line>:normalize_query`), so the
merger + frontend collapse them into a single function node with two
incoming edges.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..codebase.base import CodebaseAdapter, SymbolLocation
from ..domain import Edge, Node, Provenance, ScanError, ScanResult
from .base import ScanContext


# ---------------------------------------------------------------------------
# Per-file caches — one instance built lazily per file the scanner touches.
# Keys are file paths (str, POSIX) so they hash cheaply and dedup across
# runs. Not memoised beyond a single scan; L3 gets a fresh scanner each
# /graph call.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _SymbolEntry:
    """What an imported local name points at.

    `symbol_name is None`  ==> `local_name` refers to the WHOLE module at
    `module_dotted` (e.g. `import foo` or `from pkg import mod` where
    `mod` is a module).

    `symbol_name is not None` ==> `local_name` refers to a symbol
    (function, class, variable) named `symbol_name` inside `module_dotted`.

    Only intra-project modules are recorded — imports whose target module
    doesn't exist under the codebase root are filtered out during import
    parsing, so callers of _SymbolEntry can assume the target resolves.
    """

    module_dotted: str
    symbol_name: str | None


@dataclass(slots=True, frozen=True)
class _ClassInstance:
    """A module-level `NAME = ClassName(...)` singleton the scanner
    recognises. Enables resolving `NAME.method(...)` calls to
    `ClassName.method` in the class's home module."""

    class_name: str
    class_module_dotted: str


@dataclass(slots=True)
class _FileInfo:
    """Everything L3 needs to know about one Python file, computed once
    and reused across every function body in that file plus every
    cross-file reference into it.

    Instances are cheap to build (one AST walk) and are held in the
    scanner's `_file_cache` for the duration of one scan.
    """

    path: Path
    rel_posix: str
    module_dotted: str
    # None ==> file couldn't be parsed (syntax error, decode failure).
    # Kept as an entry (rather than absent) so we don't re-attempt.
    tree: ast.Module | None
    # Populated on demand — building requires the tree, so we lazy-init.
    imports: dict[str, _SymbolEntry] = field(default_factory=dict)
    instances: dict[str, _ClassInstance] = field(default_factory=dict)
    top_level_functions: dict[str, tuple[int, bool]] = field(default_factory=dict)  # name -> (line, is_async)
    # True once the passes above have populated the dicts.
    _built: bool = False


# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class L3CallGraphScanner:
    """Emit `calls` edges between top-level function definitions across
    the codebase. See the module docstring for the resolution surface.

    `codebase` is the same adapter L2 used — the AST cache is per-adapter,
    so parsing L2 already did is reused here. Constructed per scan; no
    persistent state.
    """

    codebase: CodebaseAdapter
    name: str = "l3_calls"

    async def scan(self, context: ScanContext) -> ScanResult:
        scanned_at = context.now()
        nodes: list[Node] = []
        edges: list[Edge] = []
        errors: list[ScanError] = []

        # Per-scan mutable state — a plain dict is fine because each scan
        # gets its own scanner instance (dataclass is frozen but we don't
        # hold this on `self`, we hold it in local scope).
        file_cache: dict[str, _FileInfo] = {}
        # Deduplicate function nodes as we emit — same (file, line,
        # qualname) identity, so the merger would collapse duplicates
        # anyway, but re-emitting is wasteful.
        seen_function_ids: set[str] = set()

        def _get_file_info(path: Path) -> _FileInfo:
            """Lazy per-scan cache — one _FileInfo per source file."""
            key = path.resolve().as_posix()
            info = file_cache.get(key)
            if info is None:
                info = self._build_file_info(path)
                file_cache[key] = info
            return info

        # Walk every Python file under root. Files that fail to parse
        # contribute nothing but don't stop the scan — real projects
        # always have at least one half-edited file.
        for py_file in self.codebase.list_files("**/*.py"):
            file_info = _get_file_info(py_file)
            if file_info.tree is None:
                continue

            # Walk each top-level function body and resolve calls.
            for stmt in file_info.tree.body:
                if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                caller_loc = SymbolLocation(
                    file=file_info.rel_posix,
                    line=stmt.lineno,
                    qualname=stmt.name,
                    is_async=isinstance(stmt, ast.AsyncFunctionDef),
                    decorators=(),
                    docstring=None,
                    kind="async_function" if isinstance(stmt, ast.AsyncFunctionDef) else "function",
                )
                caller_id = _function_node_id(caller_loc)

                # Only emit the caller node when it actually has resolved
                # outgoing calls — a leaf that calls nothing intra-project
                # is graph noise. We stage emissions and commit after the
                # walk.
                caller_edges: list[Edge] = []
                for call in _iter_calls(stmt.body):
                    callee_loc = self._resolve_call(call, file_info, _get_file_info)
                    if callee_loc is None:
                        continue
                    callee_id = _function_node_id(callee_loc)
                    caller_edges.append(
                        Edge(
                            source_id=caller_id,
                            target_id=callee_id,
                            kind="calls",
                            provenance=Provenance(
                                source=self.name,
                                source_ref=f"{file_info.rel_posix}:{call.lineno}",
                                scanned_at=scanned_at,
                            ),
                            attributes={"call_line": call.lineno},
                        )
                    )
                    # Emit the callee node (dedup across the whole scan).
                    if callee_id not in seen_function_ids:
                        nodes.append(_function_node(callee_loc, self.name, scanned_at))
                        seen_function_ids.add(callee_id)

                if caller_edges:
                    if caller_id not in seen_function_ids:
                        nodes.append(_function_node(caller_loc, self.name, scanned_at))
                        seen_function_ids.add(caller_id)
                    edges.extend(caller_edges)

        return ScanResult(scanner=self.name, nodes=nodes, edges=edges, errors=errors)

    # ------------------------------------------------------------------ resolution

    def _resolve_call(
        self,
        call: ast.Call,
        file_info: _FileInfo,
        get_file: "callable",
    ) -> SymbolLocation | None:
        """Return the callee's SymbolLocation, or None when we can't
        resolve it (dynamic, third-party, self.-call, chained, etc.).

        Never raises — resolution failures are the norm, not an error.
        """
        self._ensure_built(file_info)
        func = call.func

        # Shape 1: bare name — `foo(...)`.
        if isinstance(func, ast.Name):
            name = func.id
            # Local (same-file) top-level function?
            if name in file_info.top_level_functions:
                line, is_async = file_info.top_level_functions[name]
                return SymbolLocation(
                    file=file_info.rel_posix,
                    line=line,
                    qualname=name,
                    is_async=is_async,
                    decorators=(),
                    docstring=None,
                    kind="async_function" if is_async else "function",
                )
            # Imported name?
            entry = file_info.imports.get(name)
            if entry is not None and entry.symbol_name is not None:
                # `from x import y` — y is the callee at x.y.
                try:
                    return self.codebase.resolve_symbol(
                        f"{entry.module_dotted}.{entry.symbol_name}"
                    )
                except FileNotFoundError:
                    return None
            # Otherwise unresolved (builtin, closure, global assigned elsewhere).
            return None

        # Shape 2: attribute call — `X.Y(...)`.
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            base = func.value.id
            attr = func.attr
            entry = file_info.imports.get(base)
            if entry is not None:
                if entry.symbol_name is None:
                    # `base` is a module alias — resolve `module.attr`.
                    try:
                        return self.codebase.resolve_symbol(
                            f"{entry.module_dotted}.{attr}"
                        )
                    except FileNotFoundError:
                        return None
                # `base` is an imported name. Could be a class (constructor
                # follow-through) or a variable — either way, statically we
                # can't know its type unless we peek at the target module.
                # Delegate to the instance/class check below by loading the
                # target file's info and seeing if `base` is a singleton
                # there.
                try:
                    target_path = self._codebase_module_path(entry.module_dotted)
                except FileNotFoundError:
                    return None
                if target_path is None:
                    return None
                target_info = get_file(target_path)
                self._ensure_built(target_info)
                inst = target_info.instances.get(entry.symbol_name)
                if inst is not None:
                    try:
                        return self.codebase.resolve_class_method(
                            inst.class_module_dotted, inst.class_name, attr
                        )
                    except FileNotFoundError:
                        return None
                return None

            # `base` might be a same-file instance singleton.
            inst = file_info.instances.get(base)
            if inst is not None:
                try:
                    return self.codebase.resolve_class_method(
                        inst.class_module_dotted, inst.class_name, attr
                    )
                except FileNotFoundError:
                    return None
            return None

        # Every other Call shape (Call().foo, Subscript, self.x, etc.) is
        # unresolvable in this scanner.
        return None

    # ------------------------------------------------------------------ per-file passes

    def _build_file_info(self, path: Path) -> _FileInfo:
        """Parse + compute the dotted module path. Doesn't populate the
        import/instance tables — those are lazy via `_ensure_built`, so
        files we only *reference* (not walk) still pay just one parse."""
        # `_get_ast` is the adapter's cached parse — shared with L2.
        tree: ast.Module | None = None
        get_ast = getattr(self.codebase, "_get_ast", None)
        if callable(get_ast):
            tree = get_ast(path)
        else:
            # Fall back if the adapter doesn't expose _get_ast (a future
            # non-Local adapter might not). One-shot parse, uncached.
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError, PermissionError, OSError):
                tree = None
        rel = path.relative_to(self.codebase.root).as_posix()
        module_dotted = _path_to_module_dotted(rel)
        return _FileInfo(
            path=path,
            rel_posix=rel,
            module_dotted=module_dotted,
            tree=tree,
        )

    def _ensure_built(self, info: _FileInfo) -> None:
        """Populate imports + instances + top_level_functions on first
        access. Idempotent (guarded by `_built`)."""
        if info._built or info.tree is None:
            info._built = True
            return
        # Pass A: top-level function names (for same-file bare-name resolution).
        for node in info.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info.top_level_functions[node.name] = (
                    node.lineno,
                    isinstance(node, ast.AsyncFunctionDef),
                )
        # Pass B: imports.
        for node in info.tree.body:
            if isinstance(node, ast.Import):
                self._absorb_import(info, node)
            elif isinstance(node, ast.ImportFrom):
                self._absorb_import_from(info, node)
        # Pass C: module-level `NAME = ClassName(...)` singletons.
        for node in info.tree.body:
            if isinstance(node, ast.Assign):
                self._absorb_assign(info, node)
        info._built = True

    def _absorb_import(self, info: _FileInfo, node: ast.Import) -> None:
        # `import foo` / `import foo.bar` / `import foo as f`
        for alias in node.names:
            local = alias.asname or alias.name.split(".", 1)[0]
            # For dotted `import foo.bar`, Python binds `foo` unless there's
            # an `as`; the alias's `name` still records the full path. We
            # keep the full dotted target either way.
            module_dotted = alias.name
            if not self._is_intra_project(module_dotted):
                continue
            info.imports[local] = _SymbolEntry(module_dotted=module_dotted, symbol_name=None)

    def _absorb_import_from(self, info: _FileInfo, node: ast.ImportFrom) -> None:
        # `from foo import bar`, `from ..pkg import mod`, `from . import x`, etc.
        base_module = _resolve_relative_module(
            node.module or "",
            node.level,
            info.module_dotted,
        )
        if base_module is None:
            return  # relative import out of the project — skip
        if not self._is_intra_project(base_module):
            return
        for alias in node.names:
            local = alias.asname or alias.name
            imported = alias.name
            # Decide: is `imported` a submodule (file exists) or a name in
            # `base_module` (whatever it is)?
            child_module = f"{base_module}.{imported}"
            if self._is_intra_project(child_module):
                # It's a submodule import: `from pkg import mod` where
                # `pkg/mod.py` exists.
                info.imports[local] = _SymbolEntry(
                    module_dotted=child_module, symbol_name=None
                )
            else:
                # It's a name-in-module import.
                info.imports[local] = _SymbolEntry(
                    module_dotted=base_module, symbol_name=imported
                )

    def _absorb_assign(self, info: _FileInfo, node: ast.Assign) -> None:
        # Only handle single-target `NAME = ClassName(...)` at module scope.
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            return
        if not isinstance(node.value, ast.Call):
            return
        class_ref = node.value.func
        if isinstance(class_ref, ast.Name):
            # Local class name — look up in imports first (imported class),
            # else assume it's defined in the same file.
            class_local_name = class_ref.id
            entry = info.imports.get(class_local_name)
            if entry is not None and entry.symbol_name is not None:
                info.instances[node.targets[0].id] = _ClassInstance(
                    class_name=entry.symbol_name,
                    class_module_dotted=entry.module_dotted,
                )
            else:
                # Same-file class.
                info.instances[node.targets[0].id] = _ClassInstance(
                    class_name=class_local_name,
                    class_module_dotted=info.module_dotted,
                )
        elif isinstance(class_ref, ast.Attribute) and isinstance(class_ref.value, ast.Name):
            # `NAME = mod.ClassName()` — resolve `mod` via imports.
            base_local = class_ref.value.id
            class_name = class_ref.attr
            entry = info.imports.get(base_local)
            if entry is not None and entry.symbol_name is None:
                info.instances[node.targets[0].id] = _ClassInstance(
                    class_name=class_name,
                    class_module_dotted=entry.module_dotted,
                )
        # Anything else (`NAME = get_repo()`, `NAME: T = Class()` — AnnAssign,
        # factory funcs) is not tracked. Resolution falls through.

    # ------------------------------------------------------------------ util

    def _is_intra_project(self, dotted: str) -> bool:
        """True iff a module file exists for this dotted path under root."""
        try:
            return self._codebase_module_path(dotted) is not None
        except FileNotFoundError:
            return False

    def _codebase_module_path(self, dotted: str) -> Path | None:
        """Try to derive a file path under root for a dotted module path.
        Returns None when nothing exists there (not a FileNotFoundError —
        that's for the adapter's own resolve_symbol contract)."""
        parts = dotted.split(".")
        if not parts or "" in parts:
            return None
        for p in parts:
            if p in ("..", "."):
                return None
        candidate_file = self.codebase.root.joinpath(*parts).with_suffix(".py")
        if candidate_file.is_file():
            return candidate_file
        candidate_pkg = self.codebase.root.joinpath(*parts, "__init__.py")
        if candidate_pkg.is_file():
            return candidate_pkg
        return None


# ---------------------------------------------------------------------------
# module-scope helpers


def _function_node_id(loc: SymbolLocation) -> str:
    """Same format L2 uses — so a function L2 emits and the same function
    referenced here collapse to one node via the merger's id dedup.
    Fan-in works for free because both callers produce the same target
    id."""
    return f"code:{loc.file}:{loc.line}:{loc.qualname}"


def _function_node(
    loc: SymbolLocation, scanner_name: str, scanned_at
) -> Node:
    return Node(
        id=_function_node_id(loc),
        type="function",
        name=loc.qualname,
        provenance=Provenance(
            source=scanner_name,
            source_ref=f"{loc.file}:{loc.line}",
            scanned_at=scanned_at,
        ),
        attributes={
            "file_path": loc.file,
            "line": loc.line,
            "qualname": loc.qualname,
            "is_async": loc.is_async,
            "decorators": list(loc.decorators),
            "docstring": loc.docstring,
            "resolution_kind": loc.kind,
        },
    )


def _iter_calls(body: Iterable[ast.stmt]) -> Iterable[ast.Call]:
    """Yield every `ast.Call` reachable from a function body — recurses
    into nested Awaits, expressions, comprehensions, and control-flow
    constructs. Doesn't descend into nested `FunctionDef` bodies (those
    are their own top-level scanning target in v2 when we support them;
    for now, ignore them so their calls aren't attributed to the parent).

    Uses ast.walk for simplicity — the "don't descend into nested defs"
    rule is enforced by pruning before walking each stmt.
    """
    for stmt in body:
        # Skip nested function/class defs — their bodies are their own scope.
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                yield node


def _path_to_module_dotted(rel_posix: str) -> str:
    """`src/tools/langchain_tools.py` -> `src.tools.langchain_tools`.
    `src/services/__init__.py`      -> `src.services`.
    Assumes the input is POSIX-normalised (which the adapter guarantees)."""
    if rel_posix.endswith("/__init__.py"):
        rel_posix = rel_posix[: -len("/__init__.py")]
    elif rel_posix.endswith(".py"):
        rel_posix = rel_posix[: -len(".py")]
    return rel_posix.replace("/", ".")


def _resolve_relative_module(
    module: str, level: int, current_module_dotted: str
) -> str | None:
    """Turn a relative import spec (`from .x import y`, level=1, module="x")
    into an absolute dotted path.

    * `level=0` — absolute import, return `module` as-is.
    * `level=1` — same package, drop the file's own last segment.
    * `level=2` — parent package, drop the last two segments.
    * If the level walks past the root, return None (nothing to resolve).
    """
    if level == 0:
        return module or None
    parts = current_module_dotted.split(".")
    if level > len(parts):
        return None
    # Drop the file's own segment(s) — `level=1` means "same package as
    # this file", so we strip THIS file's name (last segment).
    base_parts = parts[: len(parts) - level]
    if module:
        base_parts.append(module)
    result = ".".join(base_parts)
    return result or None
