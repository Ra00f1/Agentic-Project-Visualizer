"""`FlaskExtractor` — recognizes `Flask`/`Blueprint` `.route(...)` decorators.

Simpler than `FastAPIExtractor`: Flask has one decorator shape
(`.route(path, methods=[...])`), no `api_route`-style verb expansion, and
the Design doesn't ask for an "is this really a Flask app?" ambiguity
check the way FastAPI's shared `get`/`post`/... attribute names do (a
bare `.route(...)` call is distinctive enough on its own).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from ._ast_utils import list_of_string_consts_kwarg, string_kwarg, string_positional
from .base import Endpoint

_DEFAULT_METHOD = "GET"


@dataclass(slots=True, frozen=True)
class _BlueprintPrefix:
    url_prefix: str


@dataclass(slots=True, frozen=True)
class FlaskExtractor:
    framework: str = "flask"

    async def extract(self, module_path: object, source: str) -> list[Endpoint]:
        del module_path  # see FastAPIExtractor.extract's docstring -- same placeholder pattern
        tree = ast.parse(source)

        prefixes = _find_blueprint_prefixes(tree)
        endpoints: list[Endpoint] = []
        for stmt in tree.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in stmt.decorator_list:
                endpoints.extend(_endpoints_for_decorator(decorator, stmt, prefixes))
        return endpoints


def _find_blueprint_prefixes(tree: ast.Module) -> dict[str, _BlueprintPrefix]:
    """Module-level `NAME = Blueprint("x", __name__, url_prefix="/x")` assignments."""
    found: dict[str, _BlueprintPrefix] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or not isinstance(stmt.value, ast.Call):
            continue
        call = stmt.value
        if not isinstance(call.func, ast.Name) or call.func.id != "Blueprint":
            continue
        url_prefix = string_kwarg(call, "url_prefix") or ""
        found[target.id] = _BlueprintPrefix(url_prefix=url_prefix)
    return found


def _endpoints_for_decorator(
    decorator: ast.expr,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    prefixes: dict[str, _BlueprintPrefix],
) -> list[Endpoint]:
    if not isinstance(decorator, ast.Call):
        return []
    func = decorator.func
    if not isinstance(func, ast.Attribute) or func.attr != "route" or not isinstance(func.value, ast.Name):
        return []

    path = string_positional(decorator, 0)
    if path is None:
        return []
    base_name = func.value.id
    prefix = prefixes.get(base_name)
    full_path = (prefix.url_prefix if prefix is not None else "") + path

    methods = list_of_string_consts_kwarg(decorator, "methods") or [_DEFAULT_METHOD]

    return [
        Endpoint(
            method=method.upper(),
            path=full_path,
            handler_qualname=fn.name,
            handler_module="",  # see FastAPIExtractor -- orchestrator fills this in
            source_line=fn.lineno,
        )
        for method in methods
    ]
