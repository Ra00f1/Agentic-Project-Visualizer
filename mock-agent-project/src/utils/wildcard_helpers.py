"""Wildcard-import target. `__all__` limits what a wildcard import pulls in;
without it, every module-level name would be dumped into the caller.
"""

__all__ = ["helper_one", "helper_two"]


def helper_one():
    return 1


def helper_two():
    return 2


def _private_helper():
    """Underscore-prefixed. Not in __all__, not exported by wildcard."""
    return -1
