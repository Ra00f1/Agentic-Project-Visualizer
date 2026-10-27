"""`apv-run` — zero-code tracing launcher (task 18).

`apv-run [options] -- <command> [args...]` runs any Python target with
tracing on and no source change: it builds a child environment carrying
`apv_trace`'s configuration plus `PYTHONPATH` pointed at `_autoload/`
(whose `sitecustomize.py` does the actual bootstrapping — see that
module and `_bootstrap.py`), then execs or spawns the target.

**Why `sitecustomize` instead of running the target in-process with
`runpy`.** `runpy` only runs ONE script in THIS process — it can't trace
a console script like `uvicorn`, and a child process the target spawns
(uvicorn `--reload`, multiprocessing) wouldn't inherit anything `runpy`
set up in-process. `PYTHONPATH` is inherited by every child process
automatically, so this covers both cases runpy would miss.

**Why not `opentelemetry-instrument`.** It loads every registered OTel
instrumentor, including HTTP/DB ones whose spans this package's processor
skips — pure overhead for nothing we use. It also `execl`s on all
platforms; on Windows that starts a new process and exits the parent,
losing the caller's exit code and console behavior.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="apv-run",
        description="Run a Python command with apv_trace tracing on, no source changes.",
    )
    parser.add_argument("--log", metavar="PATH", help="Trace log path (default: a stable live.jsonl).")
    parser.add_argument("--manifest", metavar="PATH", help="db_id manifest, as apv_trace.configure() takes.")
    parser.add_argument(
        "--oi-kinds",
        metavar="MAP",
        help='OpenInference kind overrides, e.g. "CHAIN=workflow,RETRIEVER=".',
    )
    parser.add_argument(
        "--functions",
        nargs="?",
        const=".",
        default=None,
        metavar="ROOT",
        help="Trace every project function under ROOT (default: cwd) via sys.monitoring. Python 3.12+.",
    )
    parser.add_argument("--max-calls", type=int, default=10_000, metavar="N", help="Per-function call cap.")
    parser.add_argument(
        "--no-bridge",
        action="store_true",
        help="Don't let a traced function light up the tool node that implements it.",
    )
    parser.add_argument("--disable", metavar="a,b", help="Comma-separated OpenInference instrumentor names to skip.")
    parser.add_argument("--no-openinference", action="store_true", help="Load no OpenInference instrumentor at all.")
    parser.add_argument(
        "--show-inputs",
        action="store_true",
        help="Don't default OPENINFERENCE_HIDE_INPUTS/OUTPUTS to true.",
    )
    parser.add_argument("command", nargs=argparse.PARSER, help="The command to run, e.g. -- python -m myapp.")
    return parser.parse_args(argv)


def _autoload_dir() -> str:
    return str(Path(__file__).resolve().parent / "_autoload")


def _default_log_path() -> Path:
    # Delegates to apv_trace's own public default (Task 21's D1) rather
    # than re-deriving the same formula here -- two independent copies of
    # "platformdirs.user_log_dir('apv') / 'trace' / 'live.jsonl'" is
    # exactly the drift D1 existed to kill; if that path ever changes,
    # this follows it instead of silently diverging.
    from apv_trace import default_log_path

    return default_log_path()


def _build_env(args: argparse.Namespace) -> tuple[dict[str, str], Path]:
    env = dict(os.environ)

    log_path = Path(args.log or env.get("APV_TRACE_LOG_PATH") or _default_log_path()).resolve()
    env["APV_TRACE_LOG_PATH"] = str(log_path)

    if args.manifest:
        env["APV_TRACE_MANIFEST"] = str(Path(args.manifest).resolve())

    if args.oi_kinds:
        env["APV_TRACE_OPENINFERENCE_KINDS"] = args.oi_kinds

    if args.functions is not None:
        env["APV_TRACE_FUNCTIONS_ROOT"] = str(Path(args.functions).resolve())
        env["APV_TRACE_FUNCTIONS_MAX_CALLS"] = str(args.max_calls)
        if args.no_bridge:
            env["APV_TRACE_FUNCTIONS_BRIDGE"] = "0"

    disabled = "*" if args.no_openinference else (args.disable or "")
    if disabled:
        env["APV_TRACE_DISABLED_INSTRUMENTORS"] = disabled

    env["APV_TRACE_AUTOLOAD"] = "1"

    # setdefault, not assignment: a value the user already set in their own
    # environment wins. OpenInference parses "true"/"false" case-
    # insensitively and falls back to `False` (with its own warning) for
    # anything else, so an already-present non-boolean value is left as
    # the user's problem, not silently overwritten.
    if not args.show_inputs:
        env.setdefault("OPENINFERENCE_HIDE_INPUTS", "true")
        env.setdefault("OPENINFERENCE_HIDE_OUTPUTS", "true")

    env["PYTHONPATH"] = _prepend_pythonpath(env.get("PYTHONPATH", ""), _autoload_dir())

    return env, log_path


def _prepend_pythonpath(existing: str, new_first_entry: str) -> str:
    existing_parts = existing.split(os.pathsep) if existing else []
    seen: set[str] = set()
    deduped: list[str] = []
    for part in [new_first_entry, *existing_parts]:
        if part and part not in seen:
            seen.add(part)
            deduped.append(part)
    return os.pathsep.join(deduped)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    command: list[str] = args.command
    if not command:
        print("[apv-run] error: no command given", file=sys.stderr)
        return 2

    env, log_path = _build_env(args)
    print(f"[apv-run] tracing to {log_path} (AGENTIC_DEFAULT_TRACE_LOG_PATH={log_path})", file=sys.stderr)

    resolved = shutil.which(command[0])
    if resolved is None:
        print(f"[apv-run] error: command not found: {command[0]}", file=sys.stderr)
        return 2

    if sys.platform == "win32":
        return _run_on_windows(resolved, command, env)

    # `exec_args[0]` is the typed command name, not `resolved` -- POSIX
    # `execvpe` lets argv[0] differ from the file actually exec'd. The
    # Windows branch below passes `resolved` as the subprocess's argv[0]
    # instead (no clean equivalent there -- `executable=` resets argv[0]
    # to the NEXT list element instead of leaving it, confirmed
    # empirically). Harmless either way for a Python target: `sys.argv[0]`
    # is always the script path for `python script.py`/`-m`, ignoring
    # how `python` itself was invoked.
    exec_args = [command[0], *command[1:]]
    os.execvpe(resolved, exec_args, env)
    return 0  # unreachable: execvpe replaces this process on success


def _run_on_windows(resolved: str, command: list[str], env: dict[str, str]) -> int:
    """`os.execvpe` has no Windows equivalent that preserves the parent's
    PID the way it does on POSIX, so the child runs as a real subprocess
    instead. The launcher ignores `SIGINT` while waiting so a Ctrl+C
    delivered to the shared console is handled by the child (which
    `subprocess.run` doesn't detach from the console for), not treated as
    an interrupt of the launcher's own wait.
    """
    import contextlib
    import signal
    import subprocess

    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    result = subprocess.run([resolved, *command[1:]], env=env)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
