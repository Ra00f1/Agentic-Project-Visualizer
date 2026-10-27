"""Parity test: `apv_trace._monitor`'s module-path derivation must match
`node_index._module_from_file_path` exactly (task 18 Verification 24).

`_monitor.py` can't import this app (`.claude/CLAUDE.md` §1's boundary), so
it duplicates the same `.py`-strip-then-dot-join transformation by hand --
see that module's own `module_from_path` docstring. This test is the one
place that duplication is checked, by feeding the SAME relative path
through both implementations (one starting from an absolute path + root,
the other from the relative path directly, since that's what each
function actually takes in its own context) and comparing the result.

A dev-only dependency on `apv_trace` for this import -- never a runtime
one (same boundary `test_apv_trace_contract.py` already relies on).
"""

from __future__ import annotations

import os

import pytest

from agentic_visualizer.runtime.node_index import _module_from_file_path

apv_trace_monitor = pytest.importorskip(
    "apv_trace._monitor",
    reason="requires Python 3.12+ (sys.monitoring)",
)


@pytest.mark.parametrize(
    "relative_posix_path",
    [
        "mod.py",
        "script.py",
        "pkg/mod.py",
        "pkg/sub/mod.py",
        "pkg/__init__.py",
        "pkg/sub/__init__.py",
    ],
)
def test_module_derivation_matches_node_index(relative_posix_path: str) -> None:
    expected = _module_from_file_path(relative_posix_path)
    assert expected is not None

    root = os.path.join("C:" + os.sep, "project") if os.name == "nt" else "/project"
    abs_path = os.path.join(root, *relative_posix_path.split("/"))

    actual = apv_trace_monitor.module_from_path(abs_path, root)
    assert actual == expected
