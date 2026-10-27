"""FastAPI entrypoint — the sidecar's HTTP surface.

For v1 there's exactly one endpoint: `GET /graph` returns the merged
`Graph` produced by running L1 against a user-supplied Mongo URI + DB name.

Design decisions:

* **URI + DB name as query params.** They're per-request, not per-process,
  which lines up with the "user-editable at runtime" rule from CLAUDE.md §3
  and keeps this endpoint testable without env fiddling.

* **Auth via a shared session token, not rate limiting or request IDs.**
  `TokenAuthMiddleware` (see `security.py`) plus `TrustedHostMiddleware`
  are the whole "keep bad actors out" story for this localhost-only
  sidecar — the Tauri shell generates the token per launch and hands it
  to both sides. No token configured means auth is off (dev/test only,
  see `Settings.session_token`'s docstring).

* **Errors as HTTP status codes, not as ScanErrors.** Per-item scanner
  problems become `graph.errors` in the response body (200 OK, some errors).
  Infrastructure failures — Mongo unreachable, DB doesn't exist — become
  502 Bad Gateway with a body describing the failure. That distinction is
  what lets the UI decide "partial data" vs. "the whole scan failed."
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pymongo.errors import PyMongoError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .codebase import LocalCodebaseAdapter
from .config import Settings, get_settings
from .connectors import (
    CollectionSchema,
    MongoConnector,
    close_all_clients,
)
from .domain import Edge, Graph, Node, Provenance, ScanError, ScanResult
from .runtime import (
    TRACE_METHODS,
    DeadLetterEntryOut,
    MethodConfig,
    RuntimeChannel,
    SourceStatus,
    TraceMethodInfo,
)
from .scanners import (
    DEFAULT_COLLECTION_MAPPING,
    L1EntityScanner,
    L2ToolCodeScanner,
    L3CallGraphScanner,
    L4EndpointScanner,
    ScanContext,
)
from .scanners.l1_entity import _singularize, build_mapping_from_ui
from .security import (
    TokenAuthMiddleware,
    websocket_origin_is_allowed,
    websocket_subprotocol_for_token,
)

# --- Request body for POST /graph -----------------------------------------


class CollectionMappingEntryIn(BaseModel):
    """One collection's mapping — matches the frontend's CollectionMappingEntry.

    Extra=ignore so a UI that adds new keys later doesn't break the
    endpoint; the scanner ignores anything it doesn't recognize.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    role: str
    id_field: str = Field(default="_id", alias="idField")
    name_field: str = Field(default="name", alias="nameField")
    parent_ref_fields: list[str] = Field(default_factory=list, alias="parentRefFields")


class GraphRequest(BaseModel):
    """POST /graph body. Every field maps to the equivalent GET query param
    on the compat endpoint, plus `mapping` which GET can't carry cleanly."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    uri: str | None = None
    db: str | None = None
    collections: list[str] | None = None
    codebase_root: str | None = Field(default=None, alias="codebaseRoot")
    enable_l3: bool = Field(default=True, alias="enableL3")
    enable_l4: bool = Field(default=True, alias="enableL4")
    mapping: dict[str, CollectionMappingEntryIn] | None = None


# --- Request/response bodies for the runtime-method endpoints (Task 22) ---
# No camelCase aliasing needed here -- every field name below ("id",
# "settings", "methods", "sources") is a single word, identical either way.
# `TraceMethodInfo`/`SourceStatus` (the richer, multi-word-field models
# these endpoints also return) carry their own camelCase config where they're
# defined (`runtime/methods/base.py`, `runtime/messages.py`).


class MethodConfigIn(BaseModel):
    """One entry of `PUT /runtime/config`'s `methods` list. `settings` is
    validated against that method's own `settings_model` in the handler,
    not here -- this model only has to parse the envelope."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    settings: dict[str, Any] = Field(default_factory=dict)


class RuntimeConfigRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    methods: list[MethodConfigIn]


class MethodConfigOut(BaseModel):
    """Echoes one currently-running method's id + resolved settings --
    e.g. so the Setup screen can show the ACTUAL path in use when the
    startup env-var default turned a method on before the frontend ever
    sent a `PUT`."""

    id: str
    settings: dict[str, Any]


class RuntimeConfigOut(BaseModel):
    """`GET /runtime/config`'s shape, and `PUT /runtime/config`'s response
    (the config after applying the request, not a plain echo of it --
    `apply_config` is the source of truth for what's actually running)."""

    methods: list[MethodConfigOut]
    sources: list[SourceStatus]


logger = logging.getLogger("agentic_visualizer")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan — replaces the deprecated `on_event` hooks.

    Startup: start the `RuntimeChannel`'s background loops (`app.state.runtime_channel`,
    set in `create_app`), and — only if `AGENTIC_DEFAULT_TRACE_LOG_PATH` is
    set — turn on the `apv_live_file` method pointed at it, through the same
    `apply_config` path `PUT /runtime/config` uses. Off by default, per
    CLAUDE.md §1. The Setup screen's own saved choice (sent via `PUT` once
    the app is ready) REPLACES this config wholesale, same as any other
    `apply_config` call — see `README.md`'s precedence note.

    Shutdown: stop the runtime channel (which also stops every running
    TraceSource), then release the cached motor client pools so the process
    exits cleanly instead of leaving TCP connections open until GC eventually
    runs.
    """
    runtime_channel: RuntimeChannel = app.state.runtime_channel
    await runtime_channel.start()
    settings: Settings = app.state.settings
    if settings.default_trace_log_path:
        logger.info("runtime: tailing %s", settings.default_trace_log_path)
        apv_live_file = TRACE_METHODS["apv_live_file"]
        startup_settings = apv_live_file.settings_model.model_validate(
            {"path": settings.default_trace_log_path}
        )
        await runtime_channel.apply_config([MethodConfig(id="apv_live_file", settings=startup_settings)])
    else:
        logger.info("runtime: no trace log configured (AGENTIC_DEFAULT_TRACE_LOG_PATH unset)")
    yield
    await runtime_channel.stop()
    await close_all_clients()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Factory pattern — one line of code away from spinning up a test app
    with different settings. FastAPI's `create_app` idiom.

    `settings` is injectable (task 20) rather than always going through
    env: `serve.py` builds one with a freshly-generated `session_token`
    and hands it straight in, so the token never has to round-trip
    through `AGENTIC_SESSION_TOKEN` (readable by any same-user process)
    just to reach this function. Every existing caller that still calls
    `create_app()` with no argument is unaffected — it falls back to
    `get_settings()`, same as before this parameter existed.
    """
    if settings is None:
        settings = get_settings()
    app = FastAPI(title="agentic-visualizer", version="0.1.0", lifespan=_lifespan)
    app.state.settings = settings

    # Owns NodeIndex/RuntimeStateStore/DeadLetterBuffer and the /ws/runtime
    # fan-out. Stashed on app.state (not just a closure var) because
    # `_lifespan` needs it and is defined outside this factory, receiving
    # only `app` — the standard FastAPI pattern for a resource both the
    # lifespan and route handlers need.
    runtime_channel = RuntimeChannel(orphaned_span_ttl_seconds=settings.orphaned_span_ttl_seconds)
    app.state.runtime_channel = runtime_channel

    # Middleware order, outermost first: TrustedHost -> CORS -> token auth
    # -> app. Starlette builds the stack so the LAST `add_middleware` call
    # ends up OUTERMOST (confirmed against the installed Starlette's own
    # `build_middleware_stack`), so these three calls are deliberately in
    # the REVERSE of that order. CORS has to wrap the token check so a 401
    # response still carries CORS headers -- otherwise the webview reports
    # an opaque CORS failure instead of a 401 it could act on.
    #
    # D2: no token configured means auth is OFF, not a 500 on every
    # request. `uvicorn agentic_visualizer.main:app` and this app's own
    # test suite rely on that — the installed app always launches through
    # `serve.py`, which always generates a token, so this path is never
    # reachable there.
    if settings.session_token is not None:
        app.add_middleware(TokenAuthMiddleware, token=settings.session_token.get_secret_value())
    else:
        logger.warning("running without a session token — every endpoint is open (dev/test only)")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],  # PUT: /runtime/config (Task 22)
        allow_headers=["*"],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

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
        enable_l4: Annotated[
            bool,
            Query(
                alias="enableL4",
                description=(
                    "When true (default) and a codebase root is configured, L4 "
                    "runs and emits `endpoint` nodes (FastAPI/Flask routes) and "
                    "`exposes` edges to their handler functions. Independent of "
                    "enableL3 — L4 walks the codebase itself."
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

        return await _run_scan(
            uri=uri,
            db=db,
            mapping=mapping,
            codebase_root=codebase_root,
            enable_l3=enable_l3,
            enable_l4=enable_l4,
            settings=settings,
        )

    @app.post("/graph", response_model=Graph, response_model_by_alias=True)
    async def post_graph(body: Annotated[GraphRequest, Body()]) -> Graph:
        """POST /graph — richer configuration than GET can carry in query params.

        The frontend uses this endpoint whenever it has a user-supplied
        `mapping` (per-collection role + id_field + name_field +
        parent_ref_fields). GET /graph is kept around for tests and for
        the "no mapping picked yet" case — a defaulted request that
        just uses DEFAULT_COLLECTION_MAPPING against the mock fixture.

        Same response shape and same partial-success semantics as GET:
        per-item scanner problems land in `graph.errors`; infrastructure
        failures are 502s.
        """
        # Fill defaults with the same precedence as GET query params.
        uri = body.uri or settings.default_mongo_uri
        db = body.db or "mock_agent"
        # Resolve the mapping: user-provided beats default.
        if body.mapping is not None:
            # Pydantic parsed each entry into CollectionMappingEntryIn;
            # translate to the frontend-shaped dict `build_mapping_from_ui`
            # expects (it takes plain mappings for testability).
            ui_shape = {
                name: {
                    "role": entry.role,
                    "idField": entry.id_field,
                    "nameField": entry.name_field,
                    "parentRefFields": entry.parent_ref_fields,
                }
                for name, entry in body.mapping.items()
            }
            mapping = build_mapping_from_ui(ui_shape)
            # Then apply the "user selected a subset" filter on TOP of that.
            if body.collections:
                selected = set(body.collections)
                mapping = {k: v for k, v in mapping.items() if k in selected}
        else:
            # No user mapping — default to the mock-fixture mapping,
            # filtered by the collection selection if the user provided one.
            if body.collections:
                selected = set(body.collections)
                mapping = {k: v for k, v in DEFAULT_COLLECTION_MAPPING.items() if k in selected}
            else:
                mapping = DEFAULT_COLLECTION_MAPPING

        return await _run_scan(
            uri=uri,
            db=db,
            mapping=mapping,
            codebase_root=body.codebase_root,
            enable_l3=body.enable_l3,
            enable_l4=body.enable_l4,
            settings=settings,
        )

    async def _run_scan(
        *,
        uri: str,
        db: str,
        mapping: Any,
        codebase_root: str | None,
        enable_l3: bool,
        enable_l4: bool,
        settings: Any,
    ) -> Graph:
        """Shared scan orchestration for GET and POST /graph.

        Runs L1 always, L2 when a codebase root is available, L3 after L2
        when enable_l3 is true, L4 (independently of L3 — it walks the
        codebase itself) when enable_l4 is true. Wraps everything in the
        same partial-success semantics: scanner errors become ScanErrors
        on the Graph instead of HTTP 5xx.
        """
        try:
            async with MongoConnector(uri=uri, db_name=db) as connector:
                scanner = L1EntityScanner(db_name=db, mapping=mapping)
                context = ScanContext(connector=connector)
                l1_result = await scanner.scan(context)
                results: list[ScanResult] = [l1_result]

                # L2 is optional: the setup screen may not know a codebase
                # root yet, and running only L1 is a valid mode. When a
                # root is supplied, any adapter-construction failure
                # (missing dir, not-a-dir) becomes ONE ScanError attributed
                # to the l2_tool scanner, not an HTTP 5xx — partial success
                # (L1 works, L2 didn't) is more useful than a hard failure.
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
                        # enable_l3=false for pathological codebases.
                        if enable_l3:
                            l3_scanner = L3CallGraphScanner(codebase=adapter)
                            l3_result = await l3_scanner.scan(context)
                            results.append(l3_result)
                        # L4 (endpoints) doesn't depend on L3's output --
                        # it emits its own function node per handler (see
                        # l4_endpoints/scanner.py's module docstring) --
                        # so it's independently opt-out via enable_l4.
                        if enable_l4:
                            l4_scanner = L4EndpointScanner(codebase=adapter)
                            l4_result = await l4_scanner.scan(context)
                            results.append(l4_result)
                else:
                    # "Silent skip" — emit an advisory ScanError when tools
                    # exist but no codebase root was configured. Feeds the
                    # header warnings pill so the user sees "L2 is off".
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
                                            "or pass codebaseRoot on /graph)."
                                        ),
                                    )
                                ],
                            )
                        )

                graph = Graph.from_results(results)
        except PyMongoError as exc:
            logger.exception("Mongo scan failed for db=%s uri=%s", db, uri)
            raise HTTPException(status_code=502, detail=f"Mongo error: {exc}") from exc

        # A /graph call IS "refresh" in this app -- there's no other
        # persistent, subscribable graph state to hook a RefreshCompleted
        # event off of (see runtime/channel.py's module docstring). Every
        # successful scan updates the runtime overlay's NodeIndex to match.
        # Deliberately outside the try/except above and its own try/except
        # here: the scan already succeeded and `graph` is valid -- a bug in
        # the runtime overlay (a secondary, opt-in feature) must not turn a
        # good response into an unrelated 500.
        try:
            await runtime_channel.update_graph(graph)
        except Exception:  # noqa: BLE001 - the overlay is best-effort; the scan result is not
            logger.exception("runtime: update_graph failed after a successful /graph scan")
        return graph

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

        scanned_at = datetime.now(tz=UTC)
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

    # --- /ws/runtime --------------------------------------------------
    # Streams RuntimeChannel's messages: a snapshot right after connect,
    # then deltas/dead-letter-stats/resets as they happen. No client->server
    # messages are expected; this is purely server push.
    #
    # Auth (task 20): a browser can't set a custom header on a WebSocket
    # handshake, so the token travels as a `Sec-WebSocket-Protocol` entry
    # instead of `Authorization` — see `security.py`'s module docstring.
    # Same D2 as the HTTP side: `settings.session_token is None` means
    # auth (both the origin check and the token check) is off, matching
    # every other endpoint's dev/test-only open mode.
    @app.websocket("/ws/runtime")
    async def ws_runtime(websocket: WebSocket) -> None:
        if settings.session_token is not None:
            origin = websocket.headers.get("origin")
            if not websocket_origin_is_allowed(origin, settings.cors_origins):
                await websocket.close(code=1008)
                return
            subprotocol = websocket_subprotocol_for_token(
                websocket.scope.get("subprotocols") or [], settings.session_token.get_secret_value()
            )
            if subprotocol is None:
                await websocket.close(code=1008)
                return
            await websocket.accept(subprotocol=subprotocol)
        else:
            await websocket.accept()
        subscriber = runtime_channel.subscribe()
        try:
            await websocket.send_text(runtime_channel.snapshot_message().model_dump_json(by_alias=True))
            while True:
                message = await subscriber.get()
                await websocket.send_text(message.model_dump_json(by_alias=True))
        except WebSocketDisconnect:
            pass
        finally:
            runtime_channel.unsubscribe(subscriber)

    # --- /runtime/dead-letter -------------------------------------------
    # Recent trace events NodeIndex couldn't resolve. Feeds the frontend's
    # DeadLetterDrawer (Task 4) -- Task 3 built DeadLetterBuffer but no REST
    # endpoint for it yet; its own Files section flagged this as "add if
    # missing in NN+2 -- add a follow-up if needed." Small enough, and
    # directly required by this task's own deliverable, to add here rather
    # than defer.
    @app.get(
        "/runtime/dead-letter",
        response_model=list[DeadLetterEntryOut],
        response_model_by_alias=True,
    )
    async def get_dead_letter_entries() -> list[DeadLetterEntryOut]:
        return [DeadLetterEntryOut.from_entry(e) for e in runtime_channel.dead_letter.snapshot()]

    # --- /runtime/methods, /runtime/config (Task 22) ----------------------
    # Lists the trace methods the Setup screen can offer, and reads/replaces
    # which ones are actually running. See `runtime/methods/__init__.py`'s
    # docstring for how a method gets added to TRACE_METHODS.

    @app.get("/runtime/methods", response_model=list[TraceMethodInfo], response_model_by_alias=True)
    async def get_runtime_methods() -> list[TraceMethodInfo]:
        return [method.info() for method in TRACE_METHODS.values()]

    async def _runtime_config_response() -> RuntimeConfigOut:
        methods = [
            MethodConfigOut(id=entry.id, settings=entry.settings.model_dump(mode="json", by_alias=True))
            for entry in await runtime_channel.active_methods()
        ]
        sources = await runtime_channel.source_statuses()
        return RuntimeConfigOut(methods=methods, sources=sources)

    @app.get("/runtime/config", response_model=RuntimeConfigOut, response_model_by_alias=True)
    async def get_runtime_config() -> RuntimeConfigOut:
        return await _runtime_config_response()

    @app.put("/runtime/config", response_model=RuntimeConfigOut, response_model_by_alias=True)
    async def put_runtime_config(body: Annotated[RuntimeConfigRequest, Body()]) -> RuntimeConfigOut:
        """Replaces the whole running config with `body.methods` -- not a
        merge. Unknown id, a duplicate id, or settings that fail that
        method's own `settings_model` all 422 BEFORE anything is
        started/stopped, so a bad request never leaves the channel
        half-applied."""
        seen_ids: set[str] = set()
        entries: list[MethodConfig] = []
        for item in body.methods:
            if item.id in seen_ids:
                raise HTTPException(status_code=422, detail=f"duplicate method id: {item.id!r}")
            seen_ids.add(item.id)
            method = TRACE_METHODS.get(item.id)
            if method is None:
                raise HTTPException(status_code=422, detail=f"unknown trace method id: {item.id!r}")
            try:
                settings = method.settings_model.model_validate(item.settings)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=422, detail={"id": item.id, "errors": exc.errors(include_url=False)}
                ) from exc
            entries.append(MethodConfig(id=item.id, settings=settings))

        await runtime_channel.apply_config(entries)
        return await _runtime_config_response()

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
