"""Namespace collision — this module defines `process` and there is
another module (`namespace_a`) that also defines `process`.
The Mongo `tools` collection has one row per module → the resolver must
disambiguate by module_path, not by function name alone.
"""


def process(payload: dict) -> dict:
    """Different implementation from the sibling module."""
    return {"ns": "namespace_b", "keys": list(payload.keys())}
