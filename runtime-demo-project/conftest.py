"""Makes `runtime_demo` importable from `tests/` without installing it.

This project is `package = false` (see `pyproject.toml`) — `runtime_demo/`
is a runnable script tree, not an installed package. `python -m
runtime_demo.run_demo` gets this for free (`-m` prepends the cwd to
`sys.path`); a plain `pytest` invocation doesn't, so the tests need it
done explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
