"""runtime_demo — a small, actually-runnable agentic project.

Exists to exercise the Agentic Project Visualizer's runtime-tracing overlay
end to end. This is deliberately separate from `../mock-agent-project`,
which is the golden fixture for the *static* scanners (L1-L4) and is
explicitly documented as "nothing here is meant to run" — instrumenting
and executing that fixture would conflict with its purpose. `runtime_demo`
has no seeded DB behind it, so it only exercises the module+qualname and
typed-name node-resolution strategies, not the db_id/framework_id ones.
"""
