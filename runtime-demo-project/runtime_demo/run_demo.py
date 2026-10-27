"""Entry point: `python -m runtime_demo.run_demo`.

Runs a handful of scripted support tickets through the workflow with
tracing enabled, so a fresh checkout produces a real trace log without
needing a DB or any manual setup. See ../README.md for where the log lands
and how to point the visualizer backend at it.
"""

from __future__ import annotations

import asyncio
import os

from apv_trace import configure

from .workflow import run_support_ticket_workflow

_SCRIPTED_TICKETS = [
    "My card was charged twice, please refund me.",
    "I keep getting an error when I export my report.",
    "How do I get started with the product?",
    "CRASH",  # deliberate failure path — see agent.py
]


async def _main() -> None:
    # No args: configure() resolves the shared default_log_path() (or
    # APV_TRACE_LOG_PATH) — see its docstring. Task 21's own Context: a
    # demo run this fast (34 lines) is easy to point the backend at the
    # wrong file for, so when the env var wasn't set, print the exact
    # value to set AGENTIC_DEFAULT_TRACE_LOG_PATH to rather than making the
    # reader go find it themselves.
    log_path = configure()
    print(f"[runtime-demo] tracing to {log_path}")
    if "APV_TRACE_LOG_PATH" not in os.environ:
        print(f"[runtime-demo] point the backend at it: set AGENTIC_DEFAULT_TRACE_LOG_PATH={log_path}")

    for ticket_text in _SCRIPTED_TICKETS:
        try:
            resolution = await run_support_ticket_workflow(ticket_text)
            print(f"[runtime-demo] {ticket_text!r} -> {resolution}")
        except Exception as exc:  # noqa: BLE001 - demo keeps going past the deliberate CRASH ticket
            print(f"[runtime-demo] {ticket_text!r} -> raised {exc.__class__.__name__}: {exc}")

    print(f"[runtime-demo] done. {log_path} is ready for the visualizer backend to tail.")


if __name__ == "__main__":
    asyncio.run(_main())
