"""MongoConnector integration test.

Hits the seeded `mock_agent` DB. Auto-skips if Mongo isn't reachable —
see conftest.require_mongo for details.
"""

from __future__ import annotations

import pytest

from agentic_visualizer.connectors import MongoConnector


@pytest.mark.asyncio
async def test_introspect_lists_all_fixture_collections(require_mongo) -> None:
    uri, db = require_mongo
    async with MongoConnector(uri=uri, db_name=db) as c:
        schema = await c.introspect_schema()
    names = {s.name for s in schema}
    expected = {"users", "models", "prompts", "tools", "agents", "workflows", "files"}
    # Superset check — the DB may legitimately have other collections; we only
    # assert the fixture's collections are present.
    assert expected <= names


@pytest.mark.asyncio
async def test_introspect_reports_field_names_and_counts(require_mongo) -> None:
    uri, db = require_mongo
    async with MongoConnector(uri=uri, db_name=db) as c:
        schema = await c.introspect_schema()
    by_name = {s.name: s for s in schema}
    agents = by_name["agents"]
    # Sampled fields must include the ones our L1 scanner will key off.
    for expected in ("_id", "name", "model_id", "tool_ids", "sub_agent_ids"):
        assert expected in agents.field_names, f"missing field {expected}"
    assert agents.document_count is not None
    assert agents.document_count >= 3


@pytest.mark.asyncio
async def test_stream_rows_yields_docs(require_mongo) -> None:
    uri, db = require_mongo
    async with MongoConnector(uri=uri, db_name=db) as c:
        rows: list[dict] = [row async for row in c.stream_rows("workflows")]
    # Shape assertion — the fixture's workflow count is documented in
    # test_l1_scanner.py, and coupling this connector-level test to it
    # means every fixture growth churns two files. All we care about
    # here is: the stream yielded workflow-shaped docs.
    assert len(rows) > 0, "stream_rows yielded nothing"
    for row in rows:
        assert "_id" in row
        assert "name" in row
    assert all("_id" in r for r in rows)


@pytest.mark.asyncio
async def test_close_is_idempotent(require_mongo) -> None:
    uri, db = require_mongo
    c = MongoConnector(uri=uri, db_name=db)
    await c.close()
    await c.close()  # must not raise
