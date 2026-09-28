"""Half of a circular import pair. Python resolves this at runtime by
partial-module-import; the AST walker must not go into a loop.
"""

# Deferred import to avoid true circularity at import time.
def call_from_a():
    from src.services.circular_b import from_b
    return from_b()


def from_a():
    return "a"
