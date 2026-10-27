"""Tkinter control panel for `mock-agent-project`'s simulator.

Run from `mock-agent-project/`:
    uv run python -m simulator.ui

Lists one row per module in `simulator/scripts/` (excluding `__init__.py`),
each with a "Run" button and a "Trigger error" checkbox. Clicking Run starts
that workflow's `run(fail=...)` coroutine on a background thread — see
`_run_workflow_in_background` for why: `asyncio.run()` blocks the calling
thread until the coroutine finishes, and running it on Tkinter's own main
thread would freeze the window for the whole scripted duration (up to a few
seconds per workflow). Only one workflow may run at a time (CLAUDE.md's
"never block the UI thread" doesn't forbid concurrency, but this task's own
Design section calls for serializing runs — see `.claude/Task 8.md`), so
every other Run button is disabled while one is in flight.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import os
import pkgutil
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk
from types import ModuleType

import apv_trace

from . import scripts as scripts_package

_WINDOW_SIZE = "400x500"
_MANIFEST_PATH = Path(__file__).parent / "manifest.json"


@dataclass(frozen=True, slots=True)
class _Workflow:
    """One `simulator/scripts/*.py` module, resolved to what the UI needs to run and label it."""

    module_name: str
    display_name: str
    module: ModuleType


def _resolve_log_path() -> Path:
    """`APV_TRACE_LOG_PATH` if set, else `apv_trace.default_log_path()`.

    Used to be the simulator's own fixed `simulator.jsonl`, deliberately
    NOT `apv_trace`'s built-in default -- back when that default was a
    fresh `<run_id>.jsonl` per process. Task 21's D1 made `apv_trace`'s own
    default the same kind of stable, shared file the simulator always
    wanted, so there's no reason left to keep a second, simulator-specific
    one: using `apv_trace.default_log_path()` directly means the simulator,
    `apv-run`, and any other target in this repo all agree on one path
    without the user re-pointing `AGENTIC_DEFAULT_TRACE_LOG_PATH` per tool.
    """
    override = os.environ.get("APV_TRACE_LOG_PATH")
    return Path(override) if override else apv_trace.default_log_path()


def _discover_workflows() -> list[_Workflow]:
    """Import every `simulator/scripts/*.py` module and collect its `run`/`WORKFLOW_NAME`.

    A script missing either attribute is skipped with a stderr warning rather
    than crashing the whole UI — one broken script file shouldn't take down
    the panel for the other nine (mirrors apv_trace's own "instrumentation
    must never take the target project down" posture).
    """
    workflows: list[_Workflow] = []
    for _finder, module_name, is_pkg in pkgutil.iter_modules(scripts_package.__path__):
        if is_pkg or module_name.startswith("_"):
            continue
        module = importlib.import_module(f".{module_name}", package=scripts_package.__name__)
        if not hasattr(module, "run") or not hasattr(module, "WORKFLOW_NAME"):
            print(f"[simulator.ui] skipping {module_name}: missing run() or WORKFLOW_NAME")
            continue
        workflows.append(
            _Workflow(module_name=module_name, display_name=module.WORKFLOW_NAME, module=module)
        )
    return sorted(workflows, key=lambda w: w.display_name)


class SimulatorUI:
    """Owns the Tk widget tree and the single-workflow-at-a-time run state."""

    def __init__(self, root: tk.Tk, workflows: list[_Workflow]) -> None:
        self._root = root
        self._workflows = workflows
        self._error_vars: dict[str, tk.BooleanVar] = {}
        self._run_buttons: dict[str, ttk.Button] = {}
        self._status_var = tk.StringVar(value="idle")
        self._running = False

        self._build_widgets()

    def _build_widgets(self) -> None:
        ttk.Label(self._root, text="Trace log:", font=("TkDefaultFont", 9, "bold")).pack(
            anchor="w", padx=8, pady=(8, 0)
        )
        ttk.Label(self._root, text=str(_resolve_log_path()), wraplength=380, foreground="#555").pack(
            anchor="w", padx=8, pady=(0, 4)
        )

        if not _MANIFEST_PATH.exists():
            ttk.Label(
                self._root,
                text=(
                    "No manifest.json found — events will fall back to typed-name "
                    "matching. Run: uv run python -m simulator.build_manifest"
                ),
                wraplength=380,
                foreground="#a05a00",
            ).pack(anchor="w", padx=8, pady=(0, 4))

        ttk.Separator(self._root).pack(fill="x", padx=8, pady=4)

        list_frame = ttk.Frame(self._root)
        list_frame.pack(fill="both", expand=True, padx=8)
        for workflow in self._workflows:
            self._build_row(list_frame, workflow)

        ttk.Separator(self._root).pack(fill="x", padx=8, pady=4)
        ttk.Label(self._root, textvariable=self._status_var, foreground="#333").pack(
            anchor="w", padx=8, pady=(0, 8)
        )

    def _build_row(self, parent: ttk.Frame, workflow: _Workflow) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)

        ttk.Label(row, text=workflow.display_name).pack(side="left")

        error_var = tk.BooleanVar(value=False)
        self._error_vars[workflow.module_name] = error_var
        ttk.Checkbutton(row, text="Trigger error", variable=error_var).pack(side="right", padx=(4, 0))

        run_button = ttk.Button(
            row, text="Run", command=functools.partial(self._on_run_clicked, workflow)
        )
        run_button.pack(side="right")
        self._run_buttons[workflow.module_name] = run_button

    def _on_run_clicked(self, workflow: _Workflow) -> None:
        if self._running:
            return
        fail = self._error_vars[workflow.module_name].get()
        self._set_running(workflow.display_name)
        thread = threading.Thread(
            target=self._run_workflow_in_background, args=(workflow, fail), daemon=True
        )
        thread.start()

    def _run_workflow_in_background(self, workflow: _Workflow, fail: bool) -> None:
        """Runs on the worker thread. Only touches Tk state via `after(0, ...)`.

        Tkinter is not thread-safe — every widget/state mutation must happen
        back on the main thread's event loop, which `after(0, ...)` schedules.
        """
        try:
            asyncio.run(workflow.module.run(fail=fail))
        except Exception as exc:  # noqa: BLE001 - surfaced to the status line, not re-raised
            self._root.after(0, self._set_error, str(exc))
        else:
            self._root.after(0, self._set_done)

    def _set_running(self, display_name: str) -> None:
        self._running = True
        self._status_var.set(f"running: {display_name}")
        for button in self._run_buttons.values():
            button.state(["disabled"])

    def _set_done(self) -> None:
        self._running = False
        self._status_var.set("done")
        for button in self._run_buttons.values():
            button.state(["!disabled"])

    def _set_error(self, message: str) -> None:
        self._running = False
        self._status_var.set(f"error: {message}")
        for button in self._run_buttons.values():
            button.state(["!disabled"])


def main() -> None:
    apv_trace.configure(
        log_path=_resolve_log_path(),
        run_id="simulator",
        manifest_path=_MANIFEST_PATH if _MANIFEST_PATH.exists() else None,
    )

    root = tk.Tk()
    root.title("Agentic Project Visualizer — Simulator")
    root.geometry(_WINDOW_SIZE)

    workflows = _discover_workflows()
    SimulatorUI(root, workflows)

    root.mainloop()


if __name__ == "__main__":
    main()
