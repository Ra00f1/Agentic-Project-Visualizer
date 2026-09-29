"""One-shot exporter: `TraceEvent` Pydantic model -> docs/schemas/trace-event.v1.json.

Run without `--check` after changing `agentic_visualizer.trace.events` to
regenerate the committed schema. Run with `--check` (e.g. in CI) to fail if
the committed file has drifted from what the model would generate now.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentic_visualizer.trace.events import TraceEvent

# apps/backend/scripts/export_trace_schema.py -> repo root is 3 parents up.
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "docs" / "schemas" / "trace-event.v1.json"


def _render_schema() -> str:
    schema = TraceEvent.model_json_schema()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the committed schema differs from what the model generates now.",
    )
    args = parser.parse_args()

    rendered = _render_schema()

    if args.check:
        if not SCHEMA_PATH.exists():
            print(f"[export_trace_schema] missing: {SCHEMA_PATH}", file=sys.stderr)
            return 1
        current = SCHEMA_PATH.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"[export_trace_schema] {SCHEMA_PATH} is out of date — run "
                "`python apps/backend/scripts/export_trace_schema.py` to regenerate.",
                file=sys.stderr,
            )
            return 1
        print(f"[export_trace_schema] {SCHEMA_PATH} is up to date")
        return 0

    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    # newline="" disables universal-newline translation — without it, Windows
    # silently writes CRLF, so the committed file's bytes depend on which OS
    # last regenerated it (CLAUDE.md §11: "test both line endings").
    SCHEMA_PATH.write_text(rendered, encoding="utf-8", newline="")
    print(f"[export_trace_schema] wrote {SCHEMA_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
