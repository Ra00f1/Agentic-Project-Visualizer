"""simulator — a small, actually-runnable simulator sitting next to `mock-agent-project/src`.

`src/` stays exactly what it is: a static-analysis fixture the scanners
parse, never imported or executed (see `../README.md`). This package
doesn't import from it either — it drives the SAME NAMES `src/` and the
seeded `mock_agent` Mongo DB hold, via `apv_trace`, without touching a
single file under `src/`. See `README.md` in this directory for how to run
it and `.claude/Task 8.md` for the full design rationale.
"""
