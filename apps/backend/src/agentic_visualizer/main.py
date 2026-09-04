"""FastAPI entrypoint — the sidecar's HTTP surface.

For v1 there's exactly one endpoint: `GET /graph` returns the merged
`Graph` produced by running L1 against a user-supplied Mongo URI + DB name.

Design decisions:

* **URI + DB name as query params.** They're per-request, not per-process,
  which lines up with the "user-editable at runtime" rule from CLAUDE.md §3
  and keeps this endpoint testable without env fiddling.

* **No middleware beyond CORS.** Auth, rate limiting, request ID injection,
  etc. don't apply — this is a localhost-only sidecar. The Tauri shell owns
  the "keep bad actors out" story via the shared session token, and we'll
  wire that in when the sidecar goes behind Tauri (next slice for the
  frontend half of today).

* **Errors as HTTP status codes, not as ScanErrors.** Per-item scanner
  problems become `graph.errors` in the response body (200 OK, some errors).
  Infrastructure failures — Mongo unreachable, DB doesn't exist — become
  502 Bad Gateway with a body describing the failure. That distinction is
  what lets the UI decide "partial data" vs. "the whole scan failed."
"""

from __future__ import annotations

import logging
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pymongo.errors import PyMongoError

from bson import ObjectId
from bson.errors import InvalidId

from .config import get_settings
from .connectors import (
    CollectionSchema,
    MongoConnector,
    close_all_clients,
)
from .domain import Edge, Graph, Node, Provenance
from .codebase import LocalCodebaseAdapter
from .domain import ScanError, ScanResult
from .scanners import (
    DEFAULT_COLLECTION_MAPPING,
    L1EntityScanner,
    L2ToolCodeScanner,
    L3CallGraphScanner,
    ScanContext,
)
from .scanners.l1_entity import _singularize

logger = logging.getLogger("agentic_visualizer")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan — replaces the deprecated `on_event` hooks.

    Runs once at startup (yield), and once at shutdown (after the
    yield). We only need shutdown: release the cached motor client
    pools so the process exits cleanly instead of leaving TCP
    connections open until GC eventually runs.
    """
    yield
    await close_all_clients()


def create_app() -> FastAPI:
    """Factory pattern — one line of code away from spinning up a test app
    with different settings. FastAPI's `create_app` idiom.
    """
    settings = get_settings()
    app = FastAPI(title="agentic-visualizer", version="0.1.0", lifespan=_lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe. Cheap, no dependencies."""
        return {"status": "ok"}

    @app.get(
        "/schema",
        response_model=list[CollectionSchema],
        response_model_by_alias=True,
    )
    async def get_schema(
        uri: Annotated[
            str,
            Query(description="MongoDB connection URI."),
        ] = settings.default_mongo_uri,
        db: Annotated[
            str,
            Query(description="Target MongoDB database name."),
        ] = "mock_agent",
    ) -> list[CollectionSchema]:
        """List every collection on the target DB with counts + sampled fields.

        Feeds the setup screen — the user picks which collections to scan
        before we hit the graph. We deliberately don't cache: the target DB
        can change every request, and a fresh introspect is cheap enough
        (estimated_document_count is O(1); we sample 20 docs per collection
        for field discovery).
        """
        try:
            async with MongoConnector(uri=uri, db_name=db) as connector:
                return await connector.introspect_schema()
        except PyMongoError as exc:
            logger.exception("Mongo introspect failed for db=%s uri=%s", db, uri)
            raise HTTPException(status_code=502, detail=f"Mongo error: {exc}") from exc

    # `response_model_by_alias=True` is FastAPI's default, but we set it
    # explicitly so a future FastAPI version can't silently switch to
    # snake_case and break the TS client.
    @app.get("/graph", response_model=Graph, response_model_by_alias=True)
    async def get_graph(
        uri: Annotated[
            str,
            Query(
                description="MongoDB connection URI, e.g. mongodb://localhost:27017",
                examples=["mongodb://localhost:27017"],
            ),
        ] = settings.default_mongo_uri,
        db: Annotated[
            str,
            Query(description="Target MongoDB database name.", examples=["mock_agent"]),
        ] = "mock_agent",
        collections: Annotated[
            str | None,
            Query(
                description=(
                    "Comma-separated list of collections to scan (e.g. "
                    "'agents,models,tools'). If omitted, all collections in the "
                    "default mapping are scanned. Unknown names are ignored; "
                    "refs to unscanned collections surface as ScanErrors."
                ),
                examples=["agents,models,tools,workflows,files"],
            ),
        ] = None,
        codebase_root: Annotated[
            str | None,
            Query(
                alias="codebaseRoot",
                description=(
                    "Filesystem path to the target project's source. When set, "
                    "L2 runs after L1 and resolves each tool to the code function "
                    "that implements it, emitting `function` nodes and `implements` "
                    "edges. When unset or when the path is invalid, only L1 runs "
                    "and a single `l2_tool` ScanError explains why L2 was skipped."
                ),
                examples=["C:/Porjects/Agentic Project Visualizer/mock-agent-project"],
            ),
        ] = None,
        enable_l3: Annotated[
            bool,
            Query(
                alias="enableL3",
                description=(
                    "When true (default) and a codebase root is configured, L3 "
                    "runs after L2 and emits `calls` edges between functions. "
                    "Set to false on very large codebases where the full-tree "
                    "AST walk is too slow — L1 + L2 still run."
                ),
            ),
        ] = True,
    ) -> Graph:
        """Run the L1 scanner against `db` on `uri` and return the merged graph.

        No caching, no persistence — every call is a fresh scan. That matches
        the v1 "manual Refresh" model in CLAUDE.md §6; caching arrives when
        we add the SQLite graph store in a later slice.
        """
        # Filter the default mapping by the user's selection. Anything not in
        # DEFAULT_COLLECTION_MAPPING is silently ignored — the scanner would
        # not know how to interpret an unknown collection anyway.
        if collections:
            selected = {c.strip() for c in collections.split(",") if c.strip()}
            mapping = {k: v for k, v in DEFAULT_COLLECTION_MAPPING.items() if k in selected}
        else:
            mapping = DEFAULT_COLLECTION_MAPPING

        try:
            async with MongoConnector(uri=uri, db_name=db) as connector:
                scanner = L1EntityScanner(db_name=db, mapping=mapping)
                context = ScanContext(connector=connector)
                l1_result = await scanner.scan(context)
                results: list[ScanResult] = [l1_result]

                # L2 is optional at the API level: the setup screen may not
                # know a codebase root yet, and running only L1 is a valid
                # mode. When a root is supplied, any adapter-construction
                # failure (missing dir, not-a-dir) becomes ONE ScanError
                # attributed to the l2_tool scanner, not an HTTP 5xx —
                # partial success (L1 works, L2 didn't) is more useful than
                # a hard failure.
                effective_root = codebase_root or settings.default_codebase_root
                if effective_root:
                    try:
                        adapter = LocalCodebaseAdapter(root=Path(effective_root))
                    except (FileNotFoundError, NotADirectoryError) as exc:
                        results.append(
                            ScanResult(
                                scanner="l2_tool",
                                errors=[
                                    ScanError(
                                        scanner="l2_tool",
                                        source_ref=f"codebase_root={effective_root}",
                                        message=(
                                            f"L2 skipped: cannot open codebase root "
                                            f"'{effective_root}': {exc}"
                                        ),
                                    )
                                ],
                            )
                        )
                    else:
                        l2_scanner = L2ToolCodeScanner(codebase=adapter, upstream=l1_result)
                        l2_result = await l2_scanner.scan(context)
                        results.append(l2_result)
                        # L3 (call graph) runs on the same adapter so its
                        # AST cache is warm from L2. Opt-out via
                        # ?enable_l3=false for pathological codebases.
                        if enable_l3:
                            l3_scanner = L3CallGraphScanner(codebase=adapter)
                            l3_result = await l3_scanner.scan(context)
                            results.append(l3_result)
                else:
                    # "Silent skip" is the confusing case — the frontend
                    # shows no function nodes and the operator wonders
                    # whether L2 ran. Emit one advisory ScanError when
                    # tools exist in the graph but no codebase root was
                    # configured. Feeds the header warnings pill so the
                    # user sees "L2 is off" without having to dig.
                    tool_count = sum(1 for n in l1_result.nodes if n.type == "tool")
                    if tool_count > 0:
                        results.append(
                            ScanResult(
                                scanner="l2_tool",
                                errors=[
                                    ScanError(
                                        scanner="l2_tool",
                                        source_ref="codebase_root=<unset>",
                                        message=(
                                            f"L2 not configured: {tool_count} tool(s) "
                                            "were scanned by L1 but no codebase root "
                                            "was supplied (set AGENTIC_DEFAULT_CODEBASE_ROOT "
                                            "or pass ?codebaseRoot=... on /graph)."
                                        ),
                                    )
                                ],
                            )
                        )

                return Graph.from_results(results)
        except PyMongoError as exc:
            # Log the full exception server-side (for the sidecar console) but
            # surface a tight message to the client — the frontend UI shows this
            # in a toast, not a stack trace.
            logger.exception("Mongo scan failed for db=%s uri=%s", db, uri)
            raise HTTPException(status_code=502, detail=f"Mongo error: {exc}") from exc

    # --- /graph/subtree ---------------------------------------------------
    # Lazy loader for the children of one node. Today this only knows
    # "kind=file" — the whole point of Slice B2 is that /graph never touches
    # the files collection, so the UI asks for them one workflow at a time.
    # Extending to other kinds (agents-of-workflow, tools-of-agent) later
    # slots into this same shape.
    @app.get("/graph/subtree", response_model=Graph, response_model_by_alias=True)
    async def get_subtree(
        parent_id: Annotated[
            str,
            Query(
                alias="parentId",
                description=(
                    "The Node.id of the parent whose children we want, e.g. "
                    "'mongo:mock_agent.workflows:500000000000000000000001'."
                ),
            ),
        ],
        collection: Annotated[
            str,
            Query(
                description=(
                    "The child collection to fetch from (e.g. 'files', "
                    "'documents', 'attachments'). The foreign-key field is "
                    "derived by convention: <parent_singular>_id."
                ),
            ),
        ],
        uri: Annotated[str, Query()] = settings.default_mongo_uri,
        db: Annotated[str, Query()] = "mock_agent",
    ) -> Graph:
        """Return only the children of `parent_id` of type `kind`.

        Design notes:

        * The response is a partial `Graph` — the frontend merges it into
          its local graph state. Same shape as /graph, so the client's
          typed types are reused verbatim.
        * We validate `parent_id` strictly (must match our namespaced-id
          format) instead of accepting anything the caller sends — that
          protects against a client passing raw ObjectIds by mistake.
        * Every file emitted here carries the same provenance shape L1
          would have produced. From the graph's point of view, a subtree
          fetch is indistinguishable from an eager scan — the merger
          doesn't care which endpoint the elements came from.
        """
        # Parse the parent id. Format: `mongo:<db>.<collection>:<oid>`.
        if not parent_id.startswith("mongo:"):
            raise HTTPException(status_code=400, detail=f"Invalid parentId: {parent_id!r}")
        try:
            _, ns, oid_str = parent_id.split(":", 2)
            parent_db, parent_coll = ns.split(".", 1)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Malformed parentId: {parent_id!r}") from exc
        # Foreign key convention: `<parent_singular>_id`. Matches the
        # auto-detection heuristic in scanners/l1_entity.py.
        foreign_key = f"{_singularize(parent_coll)}_id"
        try:
            parent_oid = ObjectId(oid_str)
        except InvalidId as exc:
            raise HTTPException(status_code=400, detail=f"Bad ObjectId in parentId: {oid_str!r}") from exc

        # The `db` query param must agree with the parent id's embedded db
        # — otherwise the client asked for a workflow that lives in another
        # database and we'd cheerfully return unrelated data.
        if parent_db != db:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"parentId's db {parent_db!r} does not match query param db {db!r}. "
                    f"The two must agree."
                ),
            )

        scanned_at = datetime.now(tz=timezone.utc)
        provenance_template_source = "l1_entity"

        nodes: list[Node] = []
        edges: list[Edge] = []
        try:
            async with MongoConnector(uri=uri, db_name=db) as connector:
                # Duck-typed until Connector Protocol grows find_by_field.
                find_by_field = getattr(connector, "find_by_field", None)
                if find_by_field is None:
                    raise HTTPException(
                        status_code=500,
                        detail="Connector does not support find_by_field.",
                    )
                async for doc in find_by_field(collection, foreign_key, parent_oid):
                    doc_id = doc.get("_id")
                    if doc_id is None:
                        continue
                    child_source_ref = f"{db}.{collection}:{doc_id}"
                    prov = Provenance(
                        source=provenance_template_source,
                        source_ref=child_source_ref,
                        scanned_at=scanned_at,
                    )
                    child_node_id = f"mongo:{db}.{collection}:{doc_id}"
                    name = doc.get("name") or str(doc_id)
                    # Strip _id + convert ObjectIds/dates the same way L1 does.
                    attrs: dict[str, Any] = {}
                    for k, v in doc.items():
                        if k == "_id":
                            continue
                        attrs[k] = _stringify_bson(v)
                    # Node type resolution: known collection name → its
                    # NodeType; anything else falls back to "file" for
                    # styling purposes (sky-blue rounded rect). Extend
                    # this map when a new domain concept warrants its own
                    # color/shape.
                    KNOWN_TYPES: dict[str, str] = {
                        "files": "file",
                        "workflows": "workflow",
                        "agents": "agent",
                        "models": "model",
                        "tools": "tool",
                        "prompts": "prompt",
                        "users": "user",
                    }
                    node_type = KNOWN_TYPES.get(collection, "file")
                    nodes.append(
                        Node(
                            id=child_node_id,
                            type=node_type,  # type: ignore[arg-type]
                            name=str(name),
                            provenance=prov,
                            attributes=attrs,
                        )
                    )
                    # Emit the parent→file "contains" edge (inverted, as L1
                    # would have done). The frontend needs it to populate the
                    # aggregator's children after merge. `via_field` records
                    # which foreign key we resolved against — useful for
                    # debugging.
                    edges.append(
                        Edge(
                            source_id=parent_id,
                            target_id=child_node_id,
                            kind="contains",
                            provenance=prov,
                            attributes={"via_field": foreign_key},
                        )
                    )
        except PyMongoError as exc:
            logger.exception("Subtree fetch failed for parent=%s", parent_id)
            raise HTTPException(status_code=502, detail=f"Mongo error: {exc}") from exc

        return Graph(nodes=nodes, edges=edges, errors=[])

    return app


# --- helpers --------------------------------------------------------------


def _stringify_bson(value: Any) -> Any:
    """Recursively stringify BSON ObjectIds so the response is plain JSON.

    Mirrors `scanners.l1_entity._coerce` — we don't share the function because
    that would create a scanner→endpoint import cycle for one small utility.
    If we grow more of these, they move to a shared `bson_utils` module.
    """
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, list):
        return [_stringify_bson(v) for v in value]
    if isinstance(value, dict):
        return {k: _stringify_bson(v) for k, v in value.items()}
    return value


# Module-level `app` so `uvicorn agentic_visualizer.main:app` works.
# NOT a singleton the code should reach into — it's here only for uvicorn's
# import string. Application code creates fresh apps via `create_app()`.
app = create_app()
