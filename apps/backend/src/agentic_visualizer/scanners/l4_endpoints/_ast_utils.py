"""Small `ast`-literal-extraction helpers shared by `fastapi.py` and `flask.py`.

Neither extractor needs anything beyond "pull a string constant out of
this positional/keyword argument" — factored out once both needed the
identical four functions.
"""

from __future__ import annotations

import ast


def string_const(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def string_positional(call: ast.Call, index: int) -> str | None:
    if index >= len(call.args):
        return None
    return string_const(call.args[index])


def string_kwarg(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name:
            return string_const(kw.value)
    return None


def list_of_string_consts_kwarg(call: ast.Call, name: str) -> list[str] | None:
    """`None` means either the kwarg is absent, or it's present but not a
    clean `ast.List` of string constants — this return value alone
    doesn't distinguish the two. A caller that needs to (treating
    "absent" as "use a default" and "malformed" as "skip this
    decorator") checks `any(kw.arg == name for kw in call.keywords)`
    itself; a caller that treats both the same way doesn't need to."""
    for kw in call.keywords:
        if kw.arg != name:
            continue
        if not isinstance(kw.value, ast.List):
            return None
        values: list[str] = []
        for element in kw.value.elts:
            value = string_const(element)
            if value is None:
                return None
            values.append(value)
        return values
    return None
