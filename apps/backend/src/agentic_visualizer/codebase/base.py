"""`CodebaseAdapter` — the abstraction over a source-code source.

Per CLAUDE.md §6, all code access goes through this Protocol. Adding a new
backend (GitHub via shallow clone, remote FS, another VCS host) means
implementing this interface — the scanners never talk to the filesystem
directly.

Kept as `typing.Protocol` (structural) rather than an ABC for the same
reasons `Connector` is a Protocol — no forced inheritance, plays with mypy,
third-party adapters don't need to import us.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Protocol, runtime_checkable


@dataclass(slots=True, frozen=True)
class SymbolLocation:
    """Where a named symbol lives in the codebase.

    All fields are strictly needed by later scanners:

    * `file` is stored as a POSIX-style relative string (relative to the
      adapter's root). Deterministic across Windows and Mac — a Node id
      built from this looks the same on both, which the graph merger cares
      about.
    * `line` is 1-based to match how humans and IDEs read code.
    * `qualname` for top-level defs matches the plain name (`web_search_tool`).
      For methods it becomes `ClassName.method` — we don't need this yet, but
      the field is here so L3/L4 don't have to reshape the type.
    * `is_async`, `decorators`, `docstring` are surfaced to the detail panel
      via node attributes. Cheap to extract while we're already at the AST.
    * `kind` tells the L2 scanner *how* the symbol was matched — a direct
      function definition, or a LlamaIndex `FunctionTool.from_defaults(fn=...)`
      wrapper that we unwrapped. Handy for provenance and for the future
      IDE-jump UX ("your tool's Mongo record points at a wrapper; the code
      lives at the underlying function").
    """

    file: str
    line: int
    qualname: str
    is_async: bool
    decorators: tuple[str, ...]
    docstring: str | None
    kind: Literal["function", "async_function", "llamaindex_wrapped"]


@runtime_checkable
class CodebaseAdapter(Protocol):
    """A read-only view over a body of source code.

    Every method that touches the filesystem raises `FileNotFoundError` when
    the target is missing — never returns silent `None` for "file doesn't
    exist" alongside "symbol not found." The distinction matters: an unknown
    file is an infrastructure problem the scanner surfaces as an error; an
    unknown symbol is a per-item problem the scanner surfaces as a warning.

    The four-method surface is fixed by CLAUDE.md §6. `search` and
    `list_files` are unused by L2 but are here so L3 and L4 don't have to
    widen the Protocol later — a change that would ripple through every
    adapter implementation. Callers may check `hasattr` to detect optional
    capabilities on a specific adapter, but the surface itself is stable.
    """

    root: Path
    """Absolute path this adapter reads from. `list_files` / `read_file` /
    `resolve_symbol` all resolve paths relative to this."""

    def list_files(self, glob: str = "**/*.py") -> Iterable[Path]:
        """Yield absolute paths matching `glob` under the root.

        Never yields `__pycache__` contents, dotfiles at the root, or files
        inside virtualenvs (`.venv`, `venv`). Those are noise for every
        current caller; if a caller needs them, they can override.
        """
        ...

    def read_file(self, path: Path) -> str:
        """Read a file's UTF-8 text. Path must be inside `root`."""
        ...

    def search(self, pattern: str, glob: str = "**/*.py") -> Iterable[tuple[Path, int, str]]:
        """Grep-style search. Yields `(file, line_1_indexed, match_line)`.

        L2 doesn't use this; declared for L3 (call-graph fallback) and L4
        (endpoint decorator search).
        """
        ...

    def resolve_symbol(self, dotted_path: str) -> SymbolLocation | None:
        """Resolve `pkg.module.name` to where `name` is defined.

        Returns `None` when the module exists but no such symbol lives at
        the top level. Raises `FileNotFoundError` when the module itself
        (the derived `.py` file or `__init__.py`) cannot be located — the
        two are distinct errors and the L2 scanner reports them differently.

        Only top-level function definitions and module-level assignments
        are searched. Methods on classes are out of scope for v1 (the
        fixture has none). If we hit that in a real project the failure
        mode is `None`, and the scanner emits a `ScanError` — no silent
        misresolution.
        """
        ...

    def resolve_symbol_by_name(self, name: str) -> list["SymbolLocation"]:
        """Find every top-level function named `name` anywhere under root.

        Fallback path for L2 when a tool's Mongo `import_path` is stale or
        wrong but the underlying function still exists somewhere in the
        codebase. Decouples the scanner from any assumption about folder
        or file naming — tools can live in `integrations/`,
        `agents/skills/`, or scattered across the repo.

        Returns an unordered list. Zero matches means "still unresolved."
        Multiple matches means "ambiguous — caller reports and refuses to
        pick one" (a silent guess would misroute IDE-jumps and confuse
        the operator far more than an explicit warning).
        """
        ...

    def resolve_class_method(
        self, module_dotted: str, class_name: str, method_name: str
    ) -> "SymbolLocation | None":
        """Find a method definition inside a class inside a module.

        Used by L3 when it resolves an instance-method call to a
        module-level singleton (`X = SomeClass()` followed by `X.foo()`).
        Returns the method's location with `qualname` set to
        `ClassName.method_name` so the resulting Node id uniquely
        distinguishes it from a top-level function with the same name.

        Returns None when the module, the class, or the method doesn't
        exist. Raises `FileNotFoundError` when the module file itself
        can't be located — same distinction as `resolve_symbol`.
        """
        ...
