"""Tests for `python -m agentic_visualizer.serve` (task 20's handshake entry point).

Every test here runs the module as a REAL subprocess (not an in-process
call) — the whole point of `serve.py` is the stdout handshake protocol
and the stdin-EOF shutdown, neither of which an in-process call could
exercise meaningfully.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import httpx
import pytest

_HANDSHAKE_TIMEOUT_SECONDS = 15.0
_SHUTDOWN_TIMEOUT_SECONDS = 5.0


def _read_handshake_line(proc: subprocess.Popen[str]) -> dict[str, object]:
    """Read stdout lines until the handshake arrives, or fail after the
    timeout. Returns the parsed JSON payload."""
    assert proc.stdout is not None
    deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_SECONDS
    lines_seen: list[str] = []
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                pytest.fail(f"process exited before handshake; stdout so far: {lines_seen}")
            continue
        lines_seen.append(line)
        if line.startswith("APV_BACKEND_READY "):
            return json.loads(line[len("APV_BACKEND_READY ") :])
    pytest.fail(f"no handshake within {_HANDSHAKE_TIMEOUT_SECONDS}s; stdout so far: {lines_seen}")


def test_handshake_watch_stdin_health_and_clean_shutdown() -> None:
    """Verification 7, all four parts in one process lifecycle (spawning
    a fresh `uv run` subprocess per assertion would be slow and wouldn't
    buy independence — they're genuinely sequential stages of one run)."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentic_visualizer.serve", "--watch-stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        handshake = _read_handshake_line(proc)
        assert handshake["port"] != 8765
        assert isinstance(handshake["token"], str)
        assert len(handshake["token"]) == 64
        int(handshake["token"], 16)  # must be valid hex

        response = httpx.get(
            f"http://127.0.0.1:{handshake['port']}/health",
            headers={"Authorization": f"Bearer {handshake['token']}", "Host": "127.0.0.1"},
            timeout=5.0,
        )
        assert response.status_code == 200

        assert proc.stdin is not None
        proc.stdin.close()
        exit_code = proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        assert exit_code == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)


def test_stdout_carries_only_the_handshake_line() -> None:
    """Verification 7's last part: no other line on stdout, ever."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentic_visualizer.serve", "--watch-stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stdout is not None
        deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_SECONDS
        first_line = ""
        while time.monotonic() < deadline:
            first_line = proc.stdout.readline()
            if first_line:
                break
        assert first_line.startswith("APV_BACKEND_READY ")

        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        remaining_stdout = proc.stdout.read()
        assert remaining_stdout == ""
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)


def test_token_from_env_uses_agentic_session_token() -> None:
    """Verification 8."""
    fixed_token = "c" * 64
    env = {**os.environ, "AGENTIC_SESSION_TOKEN": fixed_token}
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentic_visualizer.serve", "--token-from-env"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    try:
        handshake = _read_handshake_line(proc)
        assert handshake["token"] == fixed_token
    finally:
        proc.kill()
        proc.wait(timeout=5.0)


def test_token_from_env_without_the_env_var_fails_fast() -> None:
    env = {k: v for k, v in os.environ.items() if k != "AGENTIC_SESSION_TOKEN"}
    result = subprocess.run(
        [sys.executable, "-m", "agentic_visualizer.serve", "--token-from-env"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode == 1
    assert "AGENTIC_SESSION_TOKEN" in result.stderr
