"""Entry point: `python -m plain_app.main`. No tracing code anywhere in
this fixture -- `apv-run` is what's meant to observe it from outside."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from .agent import handle, handle_and_catch, handle_async

_VENV_LIB = Path(__file__).resolve().parent.parent / ".venv" / "lib"
sys.path.insert(0, str(_VENV_LIB))
import fake_lib  # noqa: E402


def main() -> None:
    print(handle("hello there"))
    print(asyncio.run(handle_async("hello again")))
    print(handle_and_catch("CRASH"))
    print(fake_lib.vendored_helper())


if __name__ == "__main__":
    main()
