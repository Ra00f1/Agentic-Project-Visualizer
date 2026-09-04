"""Unit tests for LocalCodebaseAdapter (no DB, no Mongo).

Uses the mock-agent-project fixture as its target codebase directly. Each
test is a single-purpose assertion about the resolver's behavior for one
of the three tool flavors + the two distractor failure modes.

We deliberately DON'T test list_files or read_file exhaustively — those
are thin wrappers over pathlib. Coverage lives with resolve_symbol
because that's where the interesting AST logic runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_visualizer.codebase import LocalCodebaseAdapter, SymbolLocation


# The fixture project sits at <repo>/mock-agent-project, three levels up
# from apps/backend/tests/. Absolute path derivation keeps tests machine-
# independent (works whether pytest is invoked from apps/backend/ or the
# repo root).
FIXTURE_ROOT = Path(__file__).parents[3] / "mock-agent-project"


@pytest.fixture(scope="module")
def adapter() -> LocalCodebaseAdapter:
    """One adapter per module — cheap to construct, no state between tests."""
    assert FIXTURE_ROOT.exists(), f"fixture missing: {FIXTURE_ROOT}"
    return LocalCodebaseAdapter(root=FIXTURE_ROOT)


# --------------------------------------------------------------------- constructor


def test_constructor_rejects_missing_root(tmp_path: Path) -> None:
    ghost = tmp_path / "does_not_exist"
    with pytest.raises(FileNotFoundError):
        LocalCodebaseAdapter(root=ghost)


def test_constructor_rejects_file_as_root(tmp_path: Path) -> None:
    f = tmp_path / "not_a_dir.txt"
    f.write_text("hello", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        LocalCodebaseAdapter(root=f)


# --------------------------------------------------------------------- resolve_symbol
# The three tool flavors — the whole point of L2 is that all three resolve.


def test_resolve_langchain_decorated_tool(adapter: LocalCodebaseAdapter) -> None:
    """`@tool def web_search_tool(...)` — direct FunctionDef with a decorator."""
    loc = adapter.resolve_symbol("src.tools.langchain_tools.web_search_tool")
    assert loc is not None
    assert loc.qualname == "web_search_tool"
    assert loc.file == "src/tools/langchain_tools.py"  # POSIX-normalised
    assert loc.line > 0
    assert loc.is_async is False
    assert "tool" in loc.decorators
    assert loc.kind == "function"


def test_resolve_langchain_called_decorator(adapter: LocalCodebaseAdapter) -> None:
    """`@tool(return_direct=True) def calculator_tool(...)` — decorator is
    a Call node, not a Name. Resolver must handle both AST shapes."""
    loc = adapter.resolve_symbol("src.tools.langchain_tools.calculator_tool")
    assert loc is not None
    assert loc.qualname == "calculator_tool"
    # The rendered decorator string carries the "(...)" tail for called forms —
    # exact assertion so future changes to _render_decorator don't silently
    # drop the distinction.
    assert any("tool(...)" == d for d in loc.decorators), loc.decorators


def test_resolve_custom_plain_function(adapter: LocalCodebaseAdapter) -> None:
    """Plain `def internal_search_tool(...)` — no decorator, no wrapper.
    The only signal it's a tool is the Mongo record — the adapter doesn't
    care about that, it just finds the def."""
    loc = adapter.resolve_symbol("src.tools.custom_tools.internal_search_tool")
    assert loc is not None
    assert loc.qualname == "internal_search_tool"
    assert loc.file == "src/tools/custom_tools.py"
    assert loc.decorators == ()
    assert loc.kind == "function"


def test_resolve_llamaindex_wrapper_unwraps_to_underlying_fn(
    adapter: LocalCodebaseAdapter,
) -> None:
    """`document_query_tool = FunctionTool.from_defaults(fn=_document_query, ...)`.

    The Mongo record points at `document_query_tool` (the module-level
    variable). The resolver should follow `fn=_document_query` and return
    the location of the UNDERLYING function definition — that's what
    actually runs when the tool is invoked, so that's where the IDE-jump
    should land.
    """
    loc = adapter.resolve_symbol("src.tools.llamaindex_tools.document_query_tool")
    assert loc is not None
    assert loc.qualname == "_document_query", (
        f"expected underlying function, got {loc.qualname}"
    )
    assert loc.file == "src/tools/llamaindex_tools.py"
    assert loc.kind == "llamaindex_wrapped"


# --------------------------------------------------------------------- failure modes


def test_resolve_missing_symbol_returns_none(adapter: LocalCodebaseAdapter) -> None:
    """Module exists (custom_tools.py) but the named function doesn't.

    Distinct from the missing-module case below — the caller uses this to
    tell "function not found" from "module not found" and emit a different
    ScanError message.
    """
    loc = adapter.resolve_symbol("src.tools.custom_tools.this_function_does_not_exist")
    assert loc is None


def test_resolve_missing_module_raises(adapter: LocalCodebaseAdapter) -> None:
    """The module (or its containing package) doesn't exist at all —
    resolver raises FileNotFoundError. Distinct from the missing-symbol
    case: this signals bad `module_path` in the Mongo record, not a bad
    `function_name`."""
    with pytest.raises(FileNotFoundError):
        adapter.resolve_symbol("src.integrations.nonexistent.phantom_function")


def test_resolve_empty_or_bare_name_returns_none(
    adapter: LocalCodebaseAdapter,
) -> None:
    """Import paths without a `.` (bare `foo`) aren't well-formed —
    return None rather than treating the whole string as a module and
    then falling over. A real tool record with `import_path=""` should
    hit the extractor's None branch and never reach here, but defense in
    depth is cheap."""
    assert adapter.resolve_symbol("") is None
    assert adapter.resolve_symbol("bare_name_no_module") is None


def test_resolve_ignores_the_distractor_undecorated_helper(
    adapter: LocalCodebaseAdapter,
) -> None:
    """`utils.decorators.looks_like_a_tool_but_isnt` is a tool-shaped
    function with no Mongo record. resolve_symbol WILL find it (the
    adapter is generic — it resolves any dotted path). The L2 scanner
    protects us by being tool-driven: it never queries this path. This
    test just documents the boundary explicitly."""
    loc = adapter.resolve_symbol(
        "src.utils.decorators.looks_like_a_tool_but_isnt"
    )
    # It exists; the adapter's job is done. The scanner's separate
    # responsibility is to NOT ask about symbols that no Mongo record
    # references — verified in the L2 scanner test.
    assert loc is not None
    assert loc.qualname == "looks_like_a_tool_but_isnt"


# --------------------------------------------------------------------- SymbolLocation
# Cheap sanity: the frozen dataclass carries what the scanner expects.


def test_symbol_location_is_hashable_and_frozen() -> None:
    """Frozen so scanners can put SymbolLocation into sets/dicts if useful
    for dedup — and so no one accidentally mutates a returned location
    (would break future memoization)."""
    a = SymbolLocation(
        file="x.py", line=1, qualname="foo",
        is_async=False, decorators=(), docstring=None, kind="function",
    )
    b = SymbolLocation(
        file="x.py", line=1, qualname="foo",
        is_async=False, decorators=(), docstring=None, kind="function",
    )
    assert hash(a) == hash(b)
    assert a == b
    with pytest.raises(Exception):  # dataclass-frozen raises FrozenInstanceError
        a.line = 2  # type: ignore[misc]



# --------------------------------------------------------------------- name-based fallback
# `resolve_symbol_by_name` is the fallback L2 uses when the dotted-path
# lookup fails. Tests use tmp_path so they don't couple to the fixture's
# specific set of functions.


def test_resolve_by_name_finds_unique_definition(tmp_path: Path) -> None:
    """Function in an unconventional folder is still found by name — that's
    the whole point of the fallback (decouples L2 from the "tools/" convention)."""
    (tmp_path / "src" / "skills").mkdir(parents=True)
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / "skills" / "__init__.py").write_text("")
    (tmp_path / "src" / "skills" / "formatting.py").write_text(
        "def format_output(items: list) -> str:\n    return str(items)\n"
    )
    adapter = LocalCodebaseAdapter(root=tmp_path)
    matches = adapter.resolve_symbol_by_name("format_output")
    assert len(matches) == 1
    assert matches[0].qualname == "format_output"
    assert matches[0].file == "src/skills/formatting.py"


def test_resolve_by_name_returns_multiple_on_ambiguity(tmp_path: Path) -> None:
    """Two files defining the same name → both returned. The scanner's
    contract is "caller decides how to disambiguate" — the adapter
    stays neutral and honest about what it found."""
    (tmp_path / "a.py").write_text("def duplicate_name() -> None:\n    pass\n")
    (tmp_path / "b.py").write_text("def duplicate_name() -> None:\n    pass\n")
    adapter = LocalCodebaseAdapter(root=tmp_path)
    matches = adapter.resolve_symbol_by_name("duplicate_name")
    assert len(matches) == 2
    files = {m.file for m in matches}
    assert files == {"a.py", "b.py"}


def test_resolve_by_name_ignores_broken_files(tmp_path: Path) -> None:
    """A syntactically broken file in the tree shouldn't crash the walk
    — every other file still gets scanned. Real projects always have
    at least one half-edited file mid-development."""
    (tmp_path / "good.py").write_text("def only_match() -> None:\n    pass\n")
    (tmp_path / "broken.py").write_text("def broken(:  # syntax error\n")
    adapter = LocalCodebaseAdapter(root=tmp_path)
    matches = adapter.resolve_symbol_by_name("only_match")
    assert len(matches) == 1
    assert matches[0].file == "good.py"


def test_resolve_by_name_zero_matches(tmp_path: Path) -> None:
    """No match → empty list, not an exception. Caller interprets zero
    as "still unresolved" and emits its own ScanError."""
    (tmp_path / "a.py").write_text("def something_else() -> None:\n    pass\n")
    adapter = LocalCodebaseAdapter(root=tmp_path)
    assert adapter.resolve_symbol_by_name("does_not_exist") == []


def test_resolve_by_name_empty_string(tmp_path: Path) -> None:
    """Empty name is a caller bug (they should have skipped fallback);
    the adapter returns [] rather than treating "" as a wildcard."""
    (tmp_path / "a.py").write_text("def x() -> None:\n    pass\n")
    adapter = LocalCodebaseAdapter(root=tmp_path)
    assert adapter.resolve_symbol_by_name("") == []


# --------------------------------------------------------------------- broadened LlamaIndex heuristic
# Beyond the original `X.from_defaults(fn=Y)` shape — the wrapper AST
# detector now also accepts positional-first-arg and direct-constructor
# forms so aliased or non-idiomatic LlamaIndex code still resolves.


def _write_module(tmp_path: Path, content: str) -> LocalCodebaseAdapter:
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / "tool_module.py").write_text(content)
    return LocalCodebaseAdapter(root=tmp_path)


def test_resolve_llamaindex_positional_arg_from_defaults(tmp_path: Path) -> None:
    """`FunctionTool.from_defaults(_underlying, name="foo")` — positional
    first arg instead of `fn=_underlying`. Some LlamaIndex code samples
    use this form."""
    adapter = _write_module(tmp_path, """
class FunctionTool:
    @classmethod
    def from_defaults(cls, *a, **kw): return cls()

def _underlying(x: int) -> int:
    return x

foo_tool = FunctionTool.from_defaults(_underlying, name="foo")
""")
    loc = adapter.resolve_symbol("src.tool_module.foo_tool")
    assert loc is not None
    assert loc.qualname == "_underlying"
    assert loc.kind == "llamaindex_wrapped"


def test_resolve_llamaindex_direct_constructor_kwarg(tmp_path: Path) -> None:
    """`FunctionTool(fn=_underlying, ...)` — constructor called directly
    without `.from_defaults`. Broadening this catches projects that
    subclass or wrap FunctionTool."""
    adapter = _write_module(tmp_path, """
class FunctionTool:
    def __init__(self, fn=None, **kw): self.fn = fn

def _underlying(x: int) -> int:
    return x

bar_tool = FunctionTool(fn=_underlying, name="bar")
""")
    loc = adapter.resolve_symbol("src.tool_module.bar_tool")
    assert loc is not None
    assert loc.qualname == "_underlying"
    assert loc.kind == "llamaindex_wrapped"


def test_resolve_wrapper_rejects_lambda(tmp_path: Path) -> None:
    """`fn=lambda: ...` isn't statically followable — resolver returns
    None so L2 emits an "unresolved" warning rather than misroute."""
    adapter = _write_module(tmp_path, """
class FunctionTool:
    @classmethod
    def from_defaults(cls, *a, **kw): return cls()

no_underlying_tool = FunctionTool.from_defaults(fn=lambda x: x)
""")
    loc = adapter.resolve_symbol("src.tool_module.no_underlying_tool")
    assert loc is None


# --------------------------------------------------------------------- AST cache
# The cache is an internal detail but its correctness affects perf
# proportionally to how many tools share a file. Verify it's actually
# populated and reused.


def test_ast_cache_reuses_parses(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "def a() -> None: pass\ndef b() -> None: pass\ndef c() -> None: pass\n"
    )
    adapter = LocalCodebaseAdapter(root=tmp_path)
    # Three resolutions hitting the same file — cache should hold one entry.
    for name in ("a", "b", "c"):
        loc = adapter.resolve_symbol_by_name(name)
        assert len(loc) == 1
    # Cache size is one entry per distinct file, not per lookup.
    assert len(adapter._ast_cache) == 1
