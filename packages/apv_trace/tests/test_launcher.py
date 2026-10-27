"""Tests for `apv-run` (task 18) — the zero-code tracing launcher.

Every test invokes `python -m apv_trace.run ...` as a real subprocess
(rather than calling `run.main()` in-process) so stdout/stderr capture is
clean and the test never risks the POSIX `execvpe` branch replacing the
pytest process itself. On Windows, `main()`'s own Windows branch spawns
the target as a THIRD subprocess underneath that — these tests exercise
the real end-to-end pipeline, not a mock of it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apv_trace.run import _autoload_dir


def _run_apv(
    args: list[str], *, env_overrides: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **(env_overrides or {})}
    return subprocess.run(
        [sys.executable, "-m", "apv_trace.run", *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(cwd) if cwd else None,
    )


def _read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_fake_instrumentor_discovered_and_instrumented(tmp_path: Path) -> None:
    """Verification 1: a fake OpenInference instrumentor on PYTHONPATH is
    discovered, passed a real SDK TracerProvider, and named in the
    summary line. A second fake with an unmet dependency is listed as
    skipped, with no ERROR-level log."""
    fake_pkg_dir = tmp_path / "fake_pkg"
    fake_pkg_dir.mkdir()
    marker_path = tmp_path / "marker.json"

    (fake_pkg_dir / "fake_oi.py").write_text(
        "import json\n"
        "\n"
        "class FakeInstrumentor:\n"
        "    def instrumentation_dependencies(self):\n"
        "        return []\n"
        "\n"
        "    def instrument(self, **kwargs):\n"
        "        from opentelemetry.sdk.trace import TracerProvider\n"
        "        provider = kwargs.get('tracer_provider')\n"
        f"        with open({str(marker_path)!r}, 'w') as f:\n"
        "            json.dump({\n"
        "                'tracer_provider_passed': provider is not None,\n"
        "                'is_sdk_provider': isinstance(provider, TracerProvider),\n"
        "            }, f)\n"
    )
    _write_fake_entry_point(fake_pkg_dir, "fake_oi", "fake", "FakeInstrumentor")

    (fake_pkg_dir / "fake_oi2.py").write_text(
        "class FakeInstrumentor2:\n"
        "    def instrumentation_dependencies(self):\n"
        "        return ['this_package_does_not_exist_xyz123']\n"
        "\n"
        "    def instrument(self, **kwargs):\n"
        "        raise AssertionError('must never be called: dependency conflict was not caught')\n"
    )
    _write_fake_entry_point(fake_pkg_dir, "fake_oi2", "fake2", "FakeInstrumentor2")

    script = tmp_path / "script.py"
    script.write_text("print('ran')\n")
    log_path = tmp_path / "trace.jsonl"

    result = _run_apv(
        ["--log", str(log_path), "--", sys.executable, str(script)],
        env_overrides={"PYTHONPATH": str(fake_pkg_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert "ran" in result.stdout
    assert "fake" in result.stderr
    assert "fake2" in result.stderr
    assert "ERROR" not in result.stderr

    marker = json.loads(marker_path.read_text())
    assert marker == {"tracer_provider_passed": True, "is_sdk_provider": True}


def _write_fake_entry_point(pkg_dir: Path, module: str, ep_name: str, cls_name: str) -> None:
    """`dist-info` must sit on the SAME `sys.path` entry as the module it
    describes for `importlib.metadata` to associate the two -- both go
    directly under `pkg_dir`, the directory this test puts on PYTHONPATH."""
    dist_info = pkg_dir / f"{module}-0.1.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {module}\nVersion: 0.1\n")
    (dist_info / "entry_points.txt").write_text(f"[openinference_instrumentor]\n{ep_name} = {module}:{cls_name}\n")


def test_exit_code_propagates_from_the_target(tmp_path: Path) -> None:
    """Verification 2."""
    script = tmp_path / "script.py"
    script.write_text("import sys\nsys.exit(3)\n")
    log_path = tmp_path / "trace.jsonl"

    result = _run_apv(["--log", str(log_path), "--no-openinference", "--", sys.executable, str(script)])

    assert result.returncode == 3


def test_missing_command_exits_2_with_a_clear_message(tmp_path: Path) -> None:
    """Verification 3."""
    log_path = tmp_path / "trace.jsonl"

    result = _run_apv(
        ["--log", str(log_path), "--no-openinference", "--", "totally_nonexistent_command_xyz"]
    )

    assert result.returncode == 2
    assert "not found" in result.stderr.lower()


def test_sitecustomize_chaining(tmp_path: Path) -> None:
    """Verification 4: a user's own `sitecustomize.py` elsewhere on
    PYTHONPATH still runs, and the bootstrap also ran."""
    user_dir = tmp_path / "user_site"
    user_dir.mkdir()
    marker = tmp_path / "chain_marker.txt"
    (user_dir / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').write('chained')\n")

    script = tmp_path / "script.py"
    script.write_text("print('ran')\n")
    log_path = tmp_path / "trace.jsonl"

    result = _run_apv(
        ["--log", str(log_path), "--no-openinference", "--", sys.executable, str(script)],
        env_overrides={"PYTHONPATH": str(user_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "chained"
    assert "instrumented:" in result.stderr  # bootstrap ran to completion


def test_bootstrap_failure_does_not_block_the_target(tmp_path: Path) -> None:
    """Verification 5: a log path that can't be opened (it's a directory)
    makes the bootstrap fail, but the target still runs and exits 0."""
    bad_log_dir = tmp_path / "log_is_a_directory"
    bad_log_dir.mkdir()
    script = tmp_path / "script.py"
    script.write_text("print('still ran')\n")

    result = _run_apv(["--log", str(bad_log_dir), "--no-openinference", "--", sys.executable, str(script)])

    assert result.returncode == 0, result.stderr
    assert "still ran" in result.stdout
    assert "bootstrap failed" in result.stderr


def test_child_process_shares_the_log_file_under_distinct_run_ids(tmp_path: Path) -> None:
    """Verification 6."""
    child_script = tmp_path / "child.py"
    child_script.write_text("def main():\n    print('child ran')\n\nmain()\n")
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import subprocess, sys\n"
        "\n"
        "def main():\n"
        f"    subprocess.run([sys.executable, {str(child_script)!r}], check=True)\n"
        "    print('parent ran')\n"
        "\n"
        "main()\n"
    )
    log_path = tmp_path / "trace.jsonl"

    result = _run_apv(
        [
            "--log",
            str(log_path),
            "--no-openinference",
            "--functions",
            str(tmp_path),
            "--",
            sys.executable,
            str(parent_script),
        ]
    )

    assert result.returncode == 0, result.stderr
    events = _read_events(log_path)
    assert events, "no trace events were written"
    run_ids = {event["run_id"] for event in events}
    assert len(run_ids) == 2


def test_privacy_env_defaults_show_and_user_override(tmp_path: Path) -> None:
    """Verification 7."""
    script = tmp_path / "script.py"
    script.write_text(
        "import os\n"
        "print(os.environ.get('OPENINFERENCE_HIDE_INPUTS'), os.environ.get('OPENINFERENCE_HIDE_OUTPUTS'))\n"
    )
    log_path = tmp_path / "trace.jsonl"

    default_result = _run_apv(["--log", str(log_path), "--no-openinference", "--", sys.executable, str(script)])
    assert "true true" in default_result.stdout

    show_result = _run_apv(
        ["--log", str(log_path), "--no-openinference", "--show-inputs", "--", sys.executable, str(script)]
    )
    assert "None None" in show_result.stdout

    user_override_result = _run_apv(
        ["--log", str(log_path), "--no-openinference", "--", sys.executable, str(script)],
        env_overrides={"OPENINFERENCE_HIDE_INPUTS": "false"},
    )
    assert "false true" in user_override_result.stdout


def test_autoload_guard_without_the_env_var(tmp_path: Path) -> None:
    """Verification 8: the autoload directory alone, without
    `APV_TRACE_AUTOLOAD=1`, must configure nothing."""
    script = tmp_path / "script.py"
    script.write_text("print('ran')\n")
    log_path = tmp_path / "trace.jsonl"

    env = {**os.environ, "PYTHONPATH": _autoload_dir(), "APV_TRACE_LOG_PATH": str(log_path)}
    env.pop("APV_TRACE_AUTOLOAD", None)

    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=30)

    assert result.returncode == 0
    assert "ran" in result.stdout
    assert not log_path.exists()


def test_no_openinference_flag_loads_nothing(tmp_path: Path) -> None:
    """Verification 9."""
    script = tmp_path / "script.py"
    script.write_text("print('ran')\n")
    log_path = tmp_path / "trace.jsonl"

    result = _run_apv(["--log", str(log_path), "--no-openinference", "--", sys.executable, str(script)])

    assert result.returncode == 0, result.stderr
    assert "instrumented: none" in result.stderr


def test_real_zero_code_langchain_smoke(tmp_path: Path) -> None:
    """Verification 10: no `configure()`/`.instrument()` call anywhere in
    the script -- `apv-run` alone is what makes this work."""
    pytest.importorskip("openinference.instrumentation.langchain")

    script = tmp_path / "script.py"
    script.write_text(
        "from langchain_core.tools import tool\n"
        "\n"
        "@tool\n"
        "def add(a: int, b: int) -> int:\n"
        '    """Add two numbers."""\n'
        "    return a + b\n"
        "\n"
        "result = add.invoke({'a': 1, 'b': 2})\n"
        "print('result:', result)\n"
    )
    log_path = tmp_path / "trace.jsonl"

    result = _run_apv(
        ["--log", str(log_path), "--", sys.executable, str(script)],
        env_overrides={"LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"},
    )

    assert result.returncode == 0, result.stderr
    assert "result: 3" in result.stdout
    events = _read_events(log_path)
    tool_events = [e for e in events if e["ref"]["name"] == "add"]
    assert [e["phase"] for e in tool_events] == ["start", "end"]
