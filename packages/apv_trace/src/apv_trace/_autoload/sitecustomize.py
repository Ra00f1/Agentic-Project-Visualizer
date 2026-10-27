"""Autoload shim: `apv-run` (task 18) puts this directory first on
`PYTHONPATH` so the interpreter imports it automatically at startup, no
target-project code change required.

**Must be the only file in this directory.** `PYTHONPATH` entries are
searched like any other `sys.path` entry — a second file here would
shadow whatever else a target project might expect to find at this
directory's level.

Never raises: a target process must start even if tracing can't. Chains
to the target's own `sitecustomize`, if it has one, so `apv-run` never
silently disables it — some system Pythons (Debian/Ubuntu) and corporate
setups ship one.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys


def _chain() -> None:
    this_dir = os.path.dirname(os.path.abspath(__file__))
    other_paths = [p for p in sys.path if p and os.path.abspath(p) != this_dir]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", other_paths)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["_apv_chained_sitecustomize"] = module
    spec.loader.exec_module(module)


try:
    from apv_trace._bootstrap import bootstrap

    bootstrap()
except Exception as exc:  # noqa: BLE001 - the target must start even if tracing can't
    print(f"[apv-run] bootstrap failed, continuing without tracing: {exc.__class__.__name__}: {exc}", file=sys.stderr)

_chain()
