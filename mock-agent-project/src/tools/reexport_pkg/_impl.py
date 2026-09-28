"""Real definition of `greet`. Imported through the package __init__."""


def greet(name: str) -> str:
    """Return a greeting. Called via a re-export in the parent __init__."""
    return f"hello, {name}"
