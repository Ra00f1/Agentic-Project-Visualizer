"""Query `mock_agent` and write `simulator/manifest.json` — the `{kind}:{name}`
→ `db_id` map `apv_trace.configure(manifest_path=...)` reads so simulator
events resolve via `NodeIndex`'s high-confidence db_id strategy instead of
falling back to typed-name matching. See `packages/apv_trace/README.md`'s
"db_id resolution via a manifest" section for the consuming side.

Usage (from `mock-agent-project/`):
    uv run python -m simulator.build_manifest --uri mongodb://localhost:27017 --db mock_agent

Re-run this any time `mock_agent` is reseeded — a `--drop` reseed assigns
fresh ObjectIds, and a stale manifest means every simulator event either
dead-letters or falls back to a lower-confidence resolution strategy (see
this task's own Risk section on manifest drift).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

_DEFAULT_MANIFEST_PATH = Path(__file__).parent / "manifest.json"

# Collection name -> the `kind` its nodes carry in the visualizer's graph
# (mirrors `DEFAULT_COLLECTION_MAPPING` in the backend, but this script has
# no reason to import the app — see apv_trace's own module docstring on
# that boundary. Just the three kinds simulator scripts ever emit.
_KIND_BY_COLLECTION = {
    "workflows": "workflow",
    "agents": "agent",
    "tools": "tool",
}


def _manifest_key(kind: str, doc: dict[str, Any]) -> str:
    """`f"{kind}:{name}"`, preferring `display_name` when present — the same
    field the visualizer's L1 scanner renders as the node's label, so a
    manifest built here matches what `simulator/scripts/*.py` actually pass
    as `simulate_agent(name=...)`/`@trace(kind="workflow", name=...)`.
    Tools have no `display_name` in this fixture, so they fall back to
    `name` (already the raw snake_case the graph renders tools as).
    """
    name = doc.get("display_name") or doc["name"]
    return f"{kind}:{name}"


def build_manifest(uri: str, db_name: str) -> dict[str, dict[str, str]]:
    client: MongoClient[dict[str, Any]] = MongoClient(uri)
    try:
        db = client[db_name]
        manifest: dict[str, dict[str, str]] = {}
        for collection_name, kind in _KIND_BY_COLLECTION.items():
            collection: Collection[dict[str, Any]] = db[collection_name]
            for doc in collection.find({}, {"name": 1, "display_name": 1}):
                manifest[_manifest_key(kind, doc)] = {"db_id": str(doc["_id"])}
        return manifest
    finally:
        client.close()


def _counts_by_kind(manifest: dict[str, dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in manifest:
        kind = key.split(":", 1)[0]
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", default="mongodb://localhost:27017", help="Mongo connection URI.")
    parser.add_argument("--db", default="mock_agent", help="Database name.")
    parser.add_argument(
        "--out", type=Path, default=_DEFAULT_MANIFEST_PATH, help="Where to write manifest.json."
    )
    args = parser.parse_args()

    # PyMongo connects lazily -- an unreachable/misconfigured Mongo surfaces
    # here, inside the `find()` iteration, as a ~30s-timeout exception with a
    # deep internal traceback. Catch it at this one call site and print a
    # short actionable message instead; this is a dev script someone runs by
    # hand after a reseed, not a service with its own retry/backoff policy.
    try:
        manifest = build_manifest(args.uri, args.db)
    except PyMongoError as exc:
        print(f"failed to read from {args.db} at {args.uri}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    counts = _counts_by_kind(manifest)
    summary = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))
    print(f"wrote {args.out} — {len(manifest)} entries ({summary})")


if __name__ == "__main__":
    main()
