"""`LocalCodebaseAdapter` — filesystem-backed `CodebaseAdapter`.

Uses only the standard library: `pathlib` for filesystem walks, `ast` for
parsing. No third-party dependency. Per CLAUDE.md §11, code parsing is
static-only — we never `import`, `exec`, or `eval` on the target codebase.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable

from .base import SymbolLocation


# Directories we never descend into. If a user has a real folder called
# `venv` that isn't a virtualenv, they can subclass to override — but
# treating this as the default is what makes `list_files` produce useful
# output on nearly every real project.
_SKIP_DIRS = frozenset({"__pycache__", ".venv", "venv", ".git", "node_modules", ".mypy_cache", ".pytest_cache"})


class LocalCodebaseAdapter:
    """Reads code from a local directory tree.

    Constructor cost is a single `Path.resolve()` call; no walking done up
    front. Every read is on demand. Instances are cheap and stateless
    beyond `self.root` — safe to construct per-request without any pooling.

    A private per-instance AST cache (`_ast_cache`) prevents redundant
    parses when many tools in the same Mongo project resolve to functions
    in the same file. Cleared on adapter GC — one adapter per scan is the
    intended lifecycle, so no invalidation logic is needed.
    """

    def __init__(self, root: Path) -> None:
        root = Path(root).resolve()
        if not root.exists():
            raise FileNotFoundError(f"codebase root does not exist: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"codebase root is not a directory: {root}")
        self.root: Path = root
        # (Absolute-path-str -> ast.Module | None). `None` means we tried
        # to parse and failed (syntax error, decode error) — cached so we
        # don't keep re-parsing broken files on every lookup.
        self._ast_cache: dict[str, ast.Module | None] = {}
        # Name -> resolved SymbolLocation list. Walking the whole codebase
        # on every fallback lookup is wasteful when a scan has multiple
        # stale tool records that all miss direct resolution. Cached per
        # adapter instance so one scan pays the walk once per name.
        self._name_cache: dict[str, list[SymbolLocation]] = {}

    # ------------------------------------------------------------------ walk
    def list_files(self, glob: str = "**/*.py") -> Iterable[Path]:
        """Yield absolute paths matching `glob`, skipping `_SKIP_DIRS`."""
        for f in self._walk(self.root, glob):
            yield f

    def _walk(self, current: Path, glob: str) -> Iterable[Path]:
        # Deterministic ordering — makes scan output stable and easier to
        # diff between runs.
        for entry in sorted(current.iterdir()):
            if entry.name in _SKIP_DIRS:
                continue
            if entry.is_dir():
                yield from self._walk(entry, glob)
            elif entry.is_file() and entry.match(glob):
                yield entry

    # ------------------------------------------------------------------ read
    def read_file(self, path: Path) -> str:
        """Read a file as UTF-8. Path must be inside `self.root`.

        Enforcing containment prevents a buggy scanner from wandering off
        the codebase via `..` — a small hardening step given CLAUDE.md §11's
        paranoid-by-default posture.
        """
        p = Path(path).resolve()
        # `is_relative_to` is 3.9+; project pin is 3.11+, so it's available.
        if not p.is_relative_to(self.root):
            raise ValueError(f"path escapes codebase root: {p}")
        return p.read_text(encoding="utf-8")

    # ------------------------------------------------------------------ grep
    def search(self, pattern: str, glob: str = "**/*.py") -> Iterable[tuple[Path, int, str]]:
        """Substring search. L2 doesn't use this — implemented for L3/L4."""
        for f in self.list_files(glob):
            try:
                text = self.read_file(f)
            except (UnicodeDecodeError, PermissionError):
                # Skip files we can't read. Real projects have binary
                # `.py` (rare) or permission-restricted files. Not fatal.
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    yield f, lineno, line

    # ------------------------------------------------------------------ resolve
    def resolve_symbol(self, dotted_path: str) -> SymbolLocation | None:
        """Resolve `pkg.module.symbol` to its definition site. See
        `CodebaseAdapter.resolve_symbol` for the contract."""
        if not dotted_path or "." not in dotted_path:
            return None
        module_dotted, _, symbol_name = dotted_path.rpartition(".")
        file_path = self._module_to_path(module_dotted)  # raises FileNotFoundError

        tree = self._get_ast(file_path)
        if tree is None:
            return None

        rel = file_path.relative_to(self.root).as_posix()

        # Pass 1: direct function definitions.
        loc = self._make_location(tree, symbol_name, rel)
        if loc is not None:
            return loc

        # Pass 2: module-level `symbol = <wrapper expression>` (LlamaIndex
        # FunctionTool and shape-alikes). Recognises three shapes:
        #   symbol = X.from_defaults(fn=<Name>, ...)
        #   symbol = X.from_defaults(<Name>, ...)   # positional first arg
        #   symbol = X(fn=<Name>, ...)              # direct constructor
        # Follows the underlying Name back to a local function definition
        # in the SAME module.
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            if node.targets[0].id != symbol_name:
                continue
            underlying = _extract_wrapper_fn_name(node.value)
            if underlying is None:
                # Assignment matched but the wrapper shape isn't one we
                # recognise. Report as unresolved rather than pointing at
                # the assignment line — the intent of L2 is "point at the
                # code that runs when the tool is invoked."
                return None
            for local in tree.body:
                if (
                    isinstance(local, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and local.name == underlying
                ):
                    return SymbolLocation(
                        file=rel,
                        line=local.lineno,
                        qualname=local.name,
                        is_async=isinstance(local, ast.AsyncFunctionDef),
                        decorators=tuple(_render_decorator(d) for d in local.decorator_list),
                        docstring=ast.get_docstring(local),
                        kind="llamaindex_wrapped",
                    )
            return None

        return None

    def resolve_symbol_by_name(self, name: str) -> list[SymbolLocation]:
        """Find every top-level function named `name` anywhere under
        `self.root`. Used as a fallback when a tool's Mongo `import_path`
        is stale or plain wrong but the underlying function still exists
        somewhere in the project — decouples L2 from any assumption about
        folder or file naming (a project's tools might live in
        `integrations/`, `agents/skills/`, or scattered across the repo).

        Returns an unordered list. Zero matches: caller reports unresolved.
        One match: caller uses it (marking `resolution_kind` so operators
        can see it was a fallback). Many matches: caller reports ambiguous
        and lists the candidates so the operator can fix the Mongo record.

        Cost is one AST parse per Python file under root, memoised by
        `_get_ast` — so repeated fallback lookups within a single scan
        share the cache and only pay for the walk (files enumerated) plus
        the AST scan per file.
        """
        if not name:
            return []
        cached = self._name_cache.get(name)
        if cached is not None:
            return cached
        matches: list[SymbolLocation] = []
        for py_file in self.list_files("**/*.py"):
            tree = self._get_ast(py_file)
            if tree is None:
                continue
            rel = py_file.relative_to(self.root).as_posix()
            loc = self._make_location(tree, name, rel)
            if loc is not None:
                matches.append(loc)
        # Cache even empty results — a name that missed once will keep
        # missing until the adapter is reconstructed (next scan).
        self._name_cache[name] = matches
        return matches

    def resolve_class_method(
        self, module_dotted: str, class_name: str, method_name: str
    ) -> SymbolLocation | None:
        """Locate `Class.method` inside `module_dotted`.

        Returns a SymbolLocation whose qualname is `ClassName.method_name`
        (dotted), so the resulting Node id uniquely distinguishes it from
        a top-level function that happens to share the method's short
        name. Returns None on any lookup miss (module found, class
        missing; class found, method missing) — L3 treats those as
        "silently unresolved" rather than warnings.
        """
        file_path = self._module_to_path(module_dotted)  # raises FileNotFoundError
        tree = self._get_ast(file_path)
        if tree is None:
            return None
        rel = file_path.relative_to(self.root).as_posix()
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name != class_name:
                continue
            for m in node.body:
                if (
                    isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and m.name == method_name
                ):
                    return SymbolLocation(
                        file=rel,
                        line=m.lineno,
                        qualname=f"{class_name}.{method_name}",
                        is_async=isinstance(m, ast.AsyncFunctionDef),
                        decorators=tuple(_render_decorator(d) for d in m.decorator_list),
                        docstring=ast.get_docstring(m),
                        kind=(
                            "async_function"
                            if isinstance(m, ast.AsyncFunctionDef)
                            else "function"
                        ),
                    )
            return None  # class found, method missing
        return None  # class missing

    # ------------------------------------------------------------------ internals
    def _module_to_path(self, module_dotted: str) -> Path:
        """`src.tools.langchain_tools` -> `<root>/src/tools/langchain_tools.py`
        (or `.../langchain_tools/__init__.py` if that's what's there).

        Raises `FileNotFoundError` if neither exists. Also raises
        `ValueError` if the resolved candidate falls outside the root — a
        defense-in-depth check against pathological dotted paths. Not
        exploitable through normal `.`-splitting (empty segments only, no
        `..`), but cheap insurance.
        """
        parts = module_dotted.split(".")
        # Reject any segment that would move us up (defense-in-depth —
        # normal dot-splitting can't produce these, but a caller might
        # construct the dotted path from user input elsewhere).
        for p in parts:
            if p in ("..", "."):
                raise ValueError(f"illegal segment in module path: {module_dotted!r}")

        candidate_file = self.root.joinpath(*parts).with_suffix(".py")
        candidate_pkg = self.root.joinpath(*parts, "__init__.py")

        for candidate in (candidate_file, candidate_pkg):
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            # Re-check containment after resolve() — symlinks in the tree
            # could otherwise point outside root.
            if not resolved.is_relative_to(self.root):
                raise ValueError(
                    f"module path resolves outside codebase root: {resolved}"
                )
            return resolved

        raise FileNotFoundError(
            f"no module '{module_dotted}' under {self.root}: "
            f"tried {candidate_file} and {candidate_pkg}"
        )

    def _get_ast(self, file_path: Path) -> ast.Module | None:
        """Return a cached ast.Module for `file_path`, or None if it can't
        be parsed. Cache is keyed by the resolved absolute path string so
        symlink aliases hit the same entry.
        """
        key = str(file_path.resolve())
        if key in self._ast_cache:
            return self._ast_cache[key]
        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(file_path))
            self._ast_cache[key] = tree
            return tree
        except (SyntaxError, UnicodeDecodeError, PermissionError, OSError):
            # A broken file in the target project is that project's
            # problem, not ours to crash on. Cache the None so repeated
            # lookups don't re-attempt.
            self._ast_cache[key] = None
            return None

    def _make_location(
        self, tree: ast.Module, symbol_name: str, rel_file: str
    ) -> SymbolLocation | None:
        """Search a parsed module's top-level statements for a matching
        function definition. Returns None when no match found.

        Extracted so both `resolve_symbol` (dotted path) and
        `resolve_symbol_by_name` (fallback) share one match-and-build
        implementation.
        """
        for node in tree.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == symbol_name
            ):
                return SymbolLocation(
                    file=rel_file,
                    line=node.lineno,
                    qualname=node.name,
                    is_async=isinstance(node, ast.AsyncFunctionDef),
                    decorators=tuple(
                        _render_decorator(d) for d in node.decorator_list
                    ),
                    docstring=ast.get_docstring(node),
                    kind=(
                        "async_function"
                        if isinstance(node, ast.AsyncFunctionDef)
                        else "function"
                    ),
                )
        return None


def _render_decorator(node: ast.expr) -> str:
    """Turn a decorator AST node into a short display string.

    We only render enough to be useful in the detail panel:
      `@tool`                     -> "tool"
      `@tool(return_direct=True)` -> "tool(...)"
      `@some.module.deco`         -> "some.module.deco"
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_render_decorator(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return f"{_render_decorator(node.func)}(...)"
    return "<decorator>"


def _extract_wrapper_fn_name(value: ast.expr) -> str | None:
    """If `value` is one of the recognized tool-wrapper shapes, return
    the underlying function name (a Name node's id). Otherwise None.

    Recognized shapes (structural — not name-of-class-based, so aliased
    imports still work):

      * `<X>.from_defaults(fn=<Name>, ...)`  — LlamaIndex FunctionTool.
      * `<X>.from_defaults(<Name>, ...)`     — same, positional first arg.
      * `<X>(fn=<Name>, ...)`                — direct constructor.
      * `<X>(<Name>, ...)`                   — direct, positional first arg.

    The class-name check is deliberately loose: we don't require the
    literal name `FunctionTool` because real projects alias imports
    (`from llama_index.core.tools import FunctionTool as FT`). The tell
    is the *shape* — a call whose first positional or `fn=`-keyword
    argument is a Name node referring to a locally-defined function.

    Rejects `fn=lambda: ...`, `fn=some.attr`, and other shapes we can't
    statically follow — those return None and become unresolved warnings.
    """
    if not isinstance(value, ast.Call):
        return None

    # Accept either a bare Name/Attribute callable — no further filtering
    # on the class name itself. Anything more restrictive breaks aliased
    # imports and unusual naming.
    if not isinstance(value.func, (ast.Name, ast.Attribute)):
        return None

    # `fn=<Name>` keyword — the idiomatic LlamaIndex form.
    for kw in value.keywords:
        if kw.arg == "fn" and isinstance(kw.value, ast.Name):
            return kw.value.id

    # First positional arg is a Name — the "no keyword" variant.
    # We only accept when the first positional is a Name (not a Call or
    # Attribute) to keep the false-positive rate low. A tool wrapper's
    # first positional is by convention the underlying callable.
    if value.args and isinstance(value.args[0], ast.Name):
        return value.args[0].id

    return None
