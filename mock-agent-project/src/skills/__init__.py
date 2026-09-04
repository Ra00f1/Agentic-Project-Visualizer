"""Skills — top-level utility functions that plug into agent pipelines.

The whole point of this module in the fixture is to prove L2's name-based
fallback: `format_output_tool` is a real tool function that DOESN'T live
under `src/tools/`. A Mongo record with a stale module_path should still
resolve to it via the codebase-wide name search.
"""
