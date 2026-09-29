"""Entry point: `python -m runtime_demo.run_demo`.

Runs a handful of scripted support tickets through the workflow with
tracing enabled, so a fresh checkout produces a real trace log without
needing a DB or any manual setup. See ../README.md for where the log lands
and how to point the visualizer backend at it.
"""

from __future__ import annotations

import asyncio

from apv_trace import configure

from .workflow import run_support_ticket_workflow

_SCRIPTED_TICKETS = [
    "My card was charged twice, please refund me.",
    "I keep getting an error when I export my report.",
    "How do I get started with the product?",
    "CRASH",  # deliberate failure path — see agent.py
]


async def _main() -> None:
    # No args: configure() resolves the default platformdirs path (or
    # APV_TRACE_LOG_PATH) and generates a run_id itself — see its docstring.
    log_path = configure()
    print(f"[runtime-demo] tracing to {log_path}")

    for ticket_text in _SCRIPTED_TICKETS:
        try:
            resolution = await run_support_ticket_workflow(ticket_text)
            print(f"[runtime-demo] {ticket_text!r} -> {resolution}")
        except Exception as exc:  # noqa: BLE001 - demo keeps going past the deliberate CRASH ticket
            print(f"[runtime-demo] {ticket_text!r} -> raised {exc.__class__.__name__}: {exc}")

    print(f"[runtime-demo] done. {log_path} is ready for the visualizer backend to tail.")


if __name__ == "__main__":
    asyncio.run(_main())
