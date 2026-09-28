"""Uses a wildcard import. AST walker should still resolve `helper_one`
and `helper_two` via __all__ (or just fall back to scanning the target
module's definitions).
"""

from src.utils.wildcard_helpers import *  # noqa: F401,F403


def use_helpers():
    return helper_one() + helper_two()  # noqa: F405 — wildcard import
