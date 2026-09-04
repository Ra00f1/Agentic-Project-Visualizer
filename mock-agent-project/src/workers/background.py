"""Background worker fixtures.

Exercises:
  * Nested function definitions (the inner `_batch` inside `scheduled_cleanup`).
  * Closures (the inner function captures `dry_run` from the enclosing scope).
  * Stacked decorators (`@timed` + `@log_boundary`).
  * A private helper that only gets called from a nested function — the call
    graph should still connect the outer function to that helper.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from ..repositories.document_repo import default_repo
from ..utils.decorators import timed


def log_boundary(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that would log entry/exit in a real system. Mock: no-op."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    return wrapper


def _purge_stale(ids: list[str]) -> int:
    """Actually delete stale docs. Mock: returns count that would be purged."""
    return len(ids)


@timed
@log_boundary
def scheduled_cleanup(dry_run: bool = True) -> dict[str, int]:
    """Nightly cleanup job. Stacked decorators are deliberate.

    Contains a nested function that closes over `dry_run`. The scanner should
    attribute calls made inside `_batch` back to `scheduled_cleanup`, not to
    a synthetic top-level `_batch`.
    """
    stale = default_repo.fetch_all(limit=500)
    stale_ids = [d.get("_id", "") for d in stale if d.get("stale")]

    def _batch(chunk: list[str]) -> int:
        # Closure over `dry_run`.
        if dry_run:
            return 0
        return _purge_stale(chunk)

    purged = _batch(stale_ids)
    return {"considered": len(stale_ids), "purged": purged}
