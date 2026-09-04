"""Idempotent MongoDB seeder for the mock-agent-project fixture.

Reads `dump.json` (MongoDB Extended JSON v2), converts `{"$oid": ...}` and
`{"$date": ...}` markers into real ObjectId / datetime instances, then upserts
every document by `_id`. Running twice in a row must produce the same DB state
as running once — that's what makes this safe to call from tests.

Design choices worth naming out loud:

  * `motor` (async) rather than `pymongo` — matches the connector stack the
    visualizer's backend will use in production, so this script doubles as an
    integration target when we build the Mongo connector.
  * We reimplement the ExtJSON→BSON walk ourselves rather than pulling in
    `bson.json_util`. Reason: `bson` ships with `pymongo`, but adding another
    dep to this fixture repo is unwarranted for ~30 lines of conversion.
  * URI and DB name come from CLI args first, env vars second, defaults last.
    Same precedence as the visualizer app will use.

Usage:
    python seed.py                                    # localhost / mock_agent
    python seed.py --uri mongodb://user:pw@host:27017 --db mydb
    MONGO_URI=... MONGO_DB=... python seed.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from bson import ObjectId
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError:  # pragma: no cover - fixture setup guidance
    sys.stderr.write(
        "This seeder needs `motor` and `pymongo`. Install with:\n"
        "    pip install motor pymongo\n"
    )
    raise


DUMP_PATH = Path(__file__).parent / "dump.json"

# Collections we manage, in insertion order. Kept explicit so the script fails
# loudly if `dump.json` gains a collection the seeder isn't aware of.
COLLECTIONS = ["users", "models", "prompts", "tools", "agents", "workflows", "files"]

# The agent that gets the bulk file generation. Must exist in dump.json.
HEAVY_AGENT_OID = "40000000000000000000000a"  # archive_curator
# Default bulk file count. The lazy-loading UX is meant to survive this
# without loading anything up front.
DEFAULT_BULK_FILES = 1200

# ---- large_scale_moderation workflow generation ---------------------------
# A whole workflow generated at seed time (not in dump.json) because its
# whole reason to exist is scale: a coordinator that delegates to N workers,
# each using the same shared model + tool. That drives the frontend's
# auto-clustering behavior (thresholds around 50 same-type children).
#
# ObjectId conventions here extend the dump.json scheme
# (0*=users, 1*=models, 2*=prompts, 3*=tools, 4*=agents, 5*=workflows,
# 6*=named files, 9*=generated files):
#   40000000000000000000_MOD_COORDINATOR  -> generated coordinator agent
#   41...                                 -> generated worker agents
#   50000000000000000000_MOD_WORKFLOW     -> generated workflow
# Anything with a `4` or `5` LEADING nibble is still an agent or workflow;
# the second nibble distinguishes hand-curated (`0`) from bulk-generated
# (`1`). Fixed strings so all cross-references stay stable across seed runs.
MOD_WORKFLOW_OID = "500000000000000000000003"
MOD_COORDINATOR_OID = "400000000000000000000100"
# Model and tool shared by the coordinator and every worker. Must exist in
# dump.json — deliberately reused so the frontend's per-consumer tool/model
# duplication is exercised at scale (61 clones of the same underlying tool).
MOD_SHARED_MODEL_OID = "100000000000000000000001"  # gpt-4o-mini
MOD_SHARED_TOOL_OID = "300000000000000000000007"   # internal_search
# Default worker count. 60 is comfortably above the 50-child clustering
# threshold on the frontend without being cartoonish.
DEFAULT_WORKERS = 60


def _revive(value: Any) -> Any:
    """Recursively convert ExtJSON markers into native Python/BSON types.

    Handled markers:
        {"$oid": "24-hex"}                 -> bson.ObjectId
        {"$date": "iso8601"}               -> datetime (tz-aware if 'Z' present)

    Everything else passes through untouched.
    """
    if isinstance(value, dict):
        if set(value.keys()) == {"$oid"}:
            return ObjectId(value["$oid"])
        if set(value.keys()) == {"$date"}:
            raw = value["$date"]
            # datetime.fromisoformat accepts "+00:00" but not "Z" until 3.11.
            iso = raw.replace("Z", "+00:00") if isinstance(raw, str) else raw
            dt = datetime.fromisoformat(iso)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return {k: _revive(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_revive(v) for v in value]
    return value


def _load_dump() -> dict[str, list[dict[str, Any]]]:
    """Load and revive the dump. Returns a mapping collection -> documents."""
    raw = json.loads(DUMP_PATH.read_text(encoding="utf-8"))
    revived = _revive(raw)
    # Drop the top-level `_meta` block; it's documentation, not data.
    revived.pop("_meta", None)
    unknown = set(revived) - set(COLLECTIONS)
    if unknown:
        raise ValueError(
            f"dump.json contains collections the seeder doesn't know about: {sorted(unknown)}. "
            f"Add them to COLLECTIONS in seed.py."
        )
    return revived


def _generate_bulk_files(agent_oid: str, count: int) -> list[dict[str, Any]]:
    """Yield `count` synthetic file documents attached to `agent_oid`.

    Each carries just enough metadata to be realistic (name, path, size,
    mime type, agent_id) but not so much that the seed step becomes slow.
    ObjectIds are deterministic — the leading prefix `9*` reserves this
    range for generated bulk data so it can't collide with hand-picked
    ids in dump.json (which use 0-6 prefixes).
    """
    now = datetime.now(timezone.utc)
    agent_ref = ObjectId(agent_oid)
    docs: list[dict[str, Any]] = []
    for i in range(1, count + 1):
        # 24-hex ObjectId with a "9" prefix + zero-padded index.
        oid_hex = f"9{i:023d}"
        docs.append(
            {
                "_id": ObjectId(oid_hex),
                "name": f"archive_{i:05d}.txt",
                "path": f"/archive/batch_{i // 100:03d}/archive_{i:05d}.txt",
                "size_bytes": 1024 * (i % 500 + 1),
                "mime_type": "text/plain",
                "owner_id": ObjectId("000000000000000000000001"),
                "agent_id": agent_ref,
                "workflow_id": None,
                "checksum_sha256": "generated",
                "created_at": now,
                "updated_at": now,
                "tags": ["generated", "archive"],
            }
        )
    return docs


# Short, non-overlapping specialties — used to give each generated worker a
# plausible display_name and description without repeating. If we ever push
# DEFAULT_WORKERS past len(_MOD_SPECIALTIES) we'll wrap; the label suffix
# (e.g. "moderator_57") always disambiguates, but keep it under 60 for now.
_MOD_SPECIALTIES = [
    "hate_speech", "spam", "phishing", "nudity", "violence",
    "self_harm", "harassment", "csam", "weapons", "drugs",
    "extremism", "misinformation", "impersonation", "doxxing", "gore",
    "copyright", "trademark", "profanity", "medical_advice", "legal_advice",
    "financial_advice", "political_content", "election_integrity", "coordinated_behavior", "bot_activity",
    "scam", "adult_content", "gambling", "child_safety", "eating_disorder",
    "suicide_ideation", "sensitive_data_leak", "pii_exposure", "trade_secret", "internal_leak",
    "brand_safety", "controversy", "sensitive_events", "geopolitical", "religion",
    "ethnic_slurs", "gender_bias", "age_discrimination", "disability_slurs", "orientation_slurs",
    "cybersecurity", "malware", "cryptojacking", "social_engineering", "insider_threat",
    "regulatory_compliance", "gdpr", "ccpa", "hipaa", "pci",
    "accessibility", "language_quality", "grammar", "readability", "sentiment",
]


def _generate_moderation_workflow(
    workflow_oid: str,
    coordinator_oid: str,
    shared_model_oid: str,
    shared_tool_oid: str,
    worker_count: int,
) -> tuple[dict, dict, list[dict]]:
    """Build (workflow_doc, coordinator_doc, [worker_docs]).

    The whole shape is assembled here so cross-references stay internally
    consistent regardless of how the caller sequences the inserts. Nothing
    in dump.json refers to these ids — the workflow lives entirely in
    generated seed data — so re-seeding always yields the same graph.
    """
    now = datetime.now(timezone.utc)
    model_ref = ObjectId(shared_model_oid)
    tool_ref = ObjectId(shared_tool_oid)
    owner_ref = ObjectId("000000000000000000000002")  # bob

    # Worker ids: 41...01 through 41...<worker_count in hex>. Zero-padded.
    workers: list[dict] = []
    worker_oids: list[ObjectId] = []
    for i in range(1, worker_count + 1):
        oid_hex = f"41{i:022x}"
        worker_oid = ObjectId(oid_hex)
        worker_oids.append(worker_oid)
        specialty = _MOD_SPECIALTIES[(i - 1) % len(_MOD_SPECIALTIES)]
        workers.append(
            {
                "_id": worker_oid,
                "name": f"moderator_{i:02d}",
                "display_name": f"Moderator {i:02d} ({specialty})",
                "description": f"Content moderator specialising in {specialty.replace('_', ' ')}.",
                "framework": "custom",
                "class_path": "src.agents.moderation.WorkerAgent",
                "model_id": model_ref,
                "system_prompt_id": None,
                "tool_ids": [tool_ref],
                "sub_agent_ids": [],
                "config": {"temperature": 0.1, "max_iterations": 2, "specialty": specialty},
                "created_by": owner_ref,
                "created_at": now,
                "updated_at": now,
                "tags": ["moderation", "worker", "generated"],
            }
        )

    coordinator = {
        "_id": ObjectId(coordinator_oid),
        "name": "moderation_coordinator",
        "display_name": "Moderation Coordinator",
        "description": (
            "Fans out incoming items to the specialist moderators and "
            "aggregates their verdicts."
        ),
        "framework": "custom",
        "class_path": "src.agents.moderation.CoordinatorAgent",
        "model_id": model_ref,
        "system_prompt_id": None,
        "tool_ids": [tool_ref],
        "sub_agent_ids": list(worker_oids),  # delegates_to all workers
        "config": {"temperature": 0.0, "max_iterations": 1, "fan_out": worker_count},
        "created_by": owner_ref,
        "created_at": now,
        "updated_at": now,
        "tags": ["moderation", "coordinator", "generated"],
    }

    workflow = {
        "_id": ObjectId(workflow_oid),
        "name": "large_scale_moderation",
        "display_name": "Large-scale moderation",
        "description": (
            "High-fanout moderation pipeline: a coordinator delegates each "
            f"incoming item to {worker_count} specialist worker agents."
        ),
        "entrypoint_agent_id": ObjectId(coordinator_oid),
        "agent_ids": [ObjectId(coordinator_oid), *worker_oids],
        "model_ids": [model_ref],
        "trigger": {"kind": "webhook"},
        "schedule": None,
        "created_by": owner_ref,
        "created_at": now,
        "updated_at": now,
        "tags": ["moderation", "high-fanout", "generated"],
    }
    return workflow, coordinator, workers


async def seed(
    uri: str,
    db_name: str,
    *,
    drop: bool = False,
    bulk_files: int = DEFAULT_BULK_FILES,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, int]:
    """Seed `db_name` on `uri`. Returns a per-collection count of upserts.

    If `drop=True`, each target collection is dropped before insert — useful
    when you're iterating on the fixture and want a clean slate. Default is
    upsert, which preserves any extra data you may have added by hand.

    `bulk_files` (default 1200) generates that many synthetic file documents
    attached to the archive_curator agent. Set to 0 to skip. Uses
    `insert_many` in chunks of 500 so seeding 10,000 files stays fast.

    `workers` (default 60) generates that many worker agents plus one
    coordinator plus the `large_scale_moderation` workflow that binds them.
    All ids are deterministic (see MOD_* constants), so re-seeding replaces
    prior generated docs in place without leaking duplicates. Set to 0 to
    skip generation entirely.
    """
    data = _load_dump()
    client: AsyncIOMotorClient = AsyncIOMotorClient(uri)
    try:
        db = client[db_name]
        counts: dict[str, int] = {}
        for name in COLLECTIONS:
            docs = data.get(name, [])
            coll = db[name]
            if drop:
                await coll.drop()
            n = 0
            for doc in docs:
                # replace_one on the _id gives us idempotent seed runs.
                result = await coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                if result.upserted_id is not None or result.modified_count:
                    n += 1
            counts[name] = n

        if bulk_files > 0:
            print(f"[seed] generating {bulk_files:,} bulk files for archive_curator...")
            bulk_docs = _generate_bulk_files(HEAVY_AGENT_OID, bulk_files)
            # Bulk insert in chunks. If we're not dropping first, remove any
            # previous run's bulk docs by their known id prefix before insert
            # — cheap, deterministic.
            files_coll = db["files"]
            if not drop:
                await files_coll.delete_many({"tags": "generated"})
            CHUNK = 500
            inserted = 0
            for i in range(0, len(bulk_docs), CHUNK):
                await files_coll.insert_many(bulk_docs[i : i + CHUNK], ordered=False)
                inserted += min(CHUNK, len(bulk_docs) - i)
            counts["files"] = counts.get("files", 0) + inserted

        if workers > 0:
            print(
                f"[seed] generating large_scale_moderation workflow: "
                f"1 coordinator + {workers} workers..."
            )
            workflow_doc, coord_doc, worker_docs = _generate_moderation_workflow(
                workflow_oid=MOD_WORKFLOW_OID,
                coordinator_oid=MOD_COORDINATOR_OID,
                shared_model_oid=MOD_SHARED_MODEL_OID,
                shared_tool_oid=MOD_SHARED_TOOL_OID,
                worker_count=workers,
            )
            agents_coll = db["agents"]
            workflows_coll = db["workflows"]
            # Idempotency: all ids are deterministic, so replace_one on _id
            # keeps repeat runs clean. We also proactively wipe any previous
            # generated worker docs whose id range no longer maps to a
            # current worker (e.g. if the caller reduced --workers).
            # Remove any stale generated workers from a prior run with a
            # larger --workers count. Match on the generated marker tag AND
            # exclude the current set of ids — narrowing by tag prevents
            # this ever touching hand-curated agents from dump.json.
            current_worker_ids = [w["_id"] for w in worker_docs]
            await agents_coll.delete_many(
                {
                    "tags": {"$all": ["moderation", "worker", "generated"]},
                    "_id": {"$nin": current_worker_ids},
                }
            )
            for doc in [coord_doc, *worker_docs]:
                r = await agents_coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                if r.upserted_id is not None or r.modified_count:
                    counts["agents"] = counts.get("agents", 0) + 1
            r = await workflows_coll.replace_one(
                {"_id": workflow_doc["_id"]}, workflow_doc, upsert=True
            )
            if r.upserted_id is not None or r.modified_count:
                counts["workflows"] = counts.get("workflows", 0) + 1

        return counts
    finally:
        client.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the mock-agent-project MongoDB fixture.")
    parser.add_argument(
        "--uri",
        default=os.environ.get("MONGO_URI", "mongodb://localhost:27017"),
        help="MongoDB connection URI (env: MONGO_URI).",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("MONGO_DB", "mock_agent"),
        help="Target database name (env: MONGO_DB).",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="Drop each collection before seeding. Off by default (upserts only).",
    )
    parser.add_argument(
        "--bulk-files",
        type=int,
        default=DEFAULT_BULK_FILES,
        help=(
            f"Generate this many synthetic files attached to the archive_curator "
            f"agent (default: {DEFAULT_BULK_FILES}). Set to 0 to skip generation."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=(
            f"Generate a large_scale_moderation workflow with this many worker "
            f"agents (default: {DEFAULT_WORKERS}). Set to 0 to skip."
        ),
    )
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    print(
        f"[seed] uri={args.uri} db={args.db} drop={args.drop} "
        f"bulk_files={args.bulk_files} workers={args.workers}"
    )
    counts = await seed(
        args.uri, args.db,
        drop=args.drop, bulk_files=args.bulk_files, workers=args.workers,
    )
    for name, n in counts.items():
        print(f"[seed]   {name:<12} upserted={n}")
    print("[seed] done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
