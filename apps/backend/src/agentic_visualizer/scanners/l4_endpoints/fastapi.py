"""`FastAPIExtractor` — recognizes `FastAPI`/`APIRouter` route decorators.

See `l4_endpoints/scanner.py`'s module docstring for the file-layout
rationale and `base.py` for the `EndpointExtractor` contract this
implements.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from ._ast_utils import list_of_string_consts_kwarg, string_kwarg, string_positional
from .base import Endpoint

_HTTP_VERBS = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})
_ROUTE_ATTRS = _HTTP_VERBS | {"api_route", "route"}


@dataclass(slots=True, frozen=True)
class _AppOrRouter:
    """A module-level `NAME = FastAPI(...)` or `NAME = APIRouter(...)` the
    extractor can confirm — `prefix` is `""` when the call had none."""

    prefix: str


@dataclass(slots=True, frozen=True)
class FastAPIExtractor:
    framework: str = "fastapi"

    async def extract(self, module_path: object, source: str) -> list[Endpoint]:
        # `module_path` isn't used here: computing a dotted module path
        # needs the codebase root, which only `L4EndpointScanner` knows.
        # `handler_module` below is a placeholder the orchestrator
        # replaces via `dataclasses.replace` after this call returns.
        del module_path
        tree = ast.parse(source)

        apps_and_routers = _find_apps_and_routers(tree)
        endpoints: list[Endpoint] = []
        for stmt in tree.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in stmt.decorator_list:
                endpoints.extend(_endpoints_for_decorator(decorator, stmt, apps_and_routers))
        return endpoints


def _find_apps_and_routers(tree: ast.Module) -> dict[str, _AppOrRouter]:
    """Module-level `NAME = FastAPI(...)` / `NAME = APIRouter(prefix=...)` assignments."""
    found: dict[str, _AppOrRouter] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or not isinstance(stmt.value, ast.Call):
            continue
        call = stmt.value
        if not isinstance(call.func, ast.Name) or call.func.id not in ("FastAPI", "APIRouter"):
            continue
        prefix = string_kwarg(call, "prefix") or ""
        found[target.id] = _AppOrRouter(prefix=prefix)
    return found


def _endpoints_for_decorator(
    decorator: ast.expr, fn: ast.FunctionDef | ast.AsyncFunctionDef, known: dict[str, _AppOrRouter]
) -> list[Endpoint]:
    if not isinstance(decorator, ast.Call):
        return []
    func = decorator.func
    if not isinstance(func, ast.Attribute) or func.attr not in _ROUTE_ATTRS or not isinstance(func.value, ast.Name):
        return []
    base_name = func.value.id
    app_or_router = known.get(base_name)
    ambiguous_reason = (
        None
        if app_or_router is not None
        else f"could not confirm {base_name!r} is a FastAPI()/APIRouter() instance"
    )
    prefix = app_or_router.prefix if app_or_router is not None else ""

    path = string_positional(decorator, 0)
    if path is None:
        return []
    full_path = prefix + path

    methods = _methods_for(decorator, func.attr)
    if methods is None:
        return []

    return [
        Endpoint(
            method=method,
            path=full_path,
            handler_qualname=fn.name,
            handler_module="",  # filled in by the orchestrator, which knows the file's dotted path
            source_line=fn.lineno,
            ambiguous_reason=ambiguous_reason,
        )
        for method in methods
    ]


def _methods_for(call: ast.Call, attr: str) -> list[str] | None:
    """The HTTP method(s) a route-shaped decorator call implies.

    `attr` in the plain verb set (`get`, `post`, ...) means exactly that
    verb. `api_route`/`route` need an explicit `methods=` kwarg — a list
    of string constants — defaulting to `["GET"]` when absent (FastAPI's
    own default). A `methods=` that isn't a clean list of string
    constants is a resolution failure for this one decorator: return
    `None` so the caller skips it rather than guessing.
    """
    if attr in _HTTP_VERBS:
        return [attr.upper()]
    methods_kwarg = list_of_string_consts_kwarg(call, "methods")
    if methods_kwarg is None:
        has_methods_kwarg = any(kw.arg == "methods" for kw in call.keywords)
        return None if has_methods_kwarg else ["GET"]
    return [m.upper() for m in methods_kwarg]
