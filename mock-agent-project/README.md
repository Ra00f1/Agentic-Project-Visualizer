# mock-agent-project

A **synthetic agentic project** used as the golden fixture for the Agentic Project
Visualizer. Nothing here is meant to *run*. Every file is a stub whose only job is
to exercise a specific shape the visualizer's scanners must handle.

Treat this as the "controlled environment" for the pipeline described in
`../CLAUDE.md` (L1 entity ingestion → L2 tool-to-function resolution → L3 call
graph → L4 endpoint mapping). When we add a new scanner rule, we add or extend a
fixture here first, then write the scanner test against it.

## Layout

```
mock-agent-project/
├── src/                       Mock Python codebase
│   ├── main.py                FastAPI app entrypoint (L4)
│   ├── api/
│   │   ├── routes.py          APIRouter with @get/@post, path params, Depends (L4)
│   │   └── flask_admin.py     Flask blueprint — proves EndpointExtractor is pluggable (L4)
│   ├── agents/
│   │   ├── research_agent.py  Wraps LangChain-style tools
│   │   ├── writer_agent.py    Wraps LlamaIndex-style tools
│   │   └── coordinator_agent.py Wraps custom-framework tools; class methods (L1, L3)
│   ├── tools/
│   │   ├── langchain_tools.py @tool decorator (L2 — LangChain adapter)
│   │   ├── llamaindex_tools.py FunctionTool.from_defaults (L2 — LlamaIndex adapter)
│   │   ├── custom_tools.py    Plain-python tools (L2 — default adapter)
│   │   └── shared.py          Helper called by tools in multiple flavors (L3 fan-in)
│   ├── services/
│   │   ├── search_service.py  Sync service layer (L3)
│   │   └── llm_service.py     Async service — TaskGroup usage (L3)
│   ├── repositories/
│   │   └── document_repo.py   Repository pattern; end of the handler→service→repo chain (L3)
│   ├── models/
│   │   └── schemas.py         Pydantic models used by the API (L4 request/response shapes)
│   ├── utils/
│   │   ├── decorators.py      Custom decorator + functools.wraps wrapper
│   │   └── dynamic_loader.py  Intentional __import__ case — KNOWN scanner limitation
│   └── workers/
│       └── background.py      Nested functions, closures, stacked decorators
└── mongo/
    ├── dump.json              Raw seed data — importable via mongoimport
    └── seed.py                Idempotent motor-based seeder (also our L1 test target)
```

## What each fixture proves

### L1 — Entity ingestion (Mongo)
`mongo/dump.json` seeds 7 collections with realistic cross-references
(`workflow.agent_ids → agents`, `agent.model_id → models`, `agent.tool_ids → tools`,
`prompt.owner_id → users`, etc.). The scanner should produce nodes for each doc
and edges for each reference.

### L2 — Tool → function resolution
Each `tools` document in Mongo has `function_name` + `module_path`. Those point
into `src/tools/*.py`. Coverage:

- **LangChain tools** are `@tool`-decorated — the decorator changes the runtime
  object but not the AST name we resolve against.
- **LlamaIndex tools** are constructed via `FunctionTool.from_defaults(fn=...)`.
  The Mongo record's `function_name` is the wrapped function's name, not the
  variable holding the `FunctionTool`.
- **Custom tools** are plain functions — the "no-framework" baseline.
- One Mongo tool row (`missing_impl_tool`) points at a function that does not
  exist in the code. Scanner should surface this as an *unresolved* edge, not crash.
- One code function (`utils.decorators.looks_like_a_tool_but_isnt`) has a
  tool-shaped signature but no Mongo row referring to it. It should NOT appear
  as a tool node.

### L3 — Function call graph
- `api/routes.py:list_documents` → `services/search_service.py:search` →
  `repositories/document_repo.py:fetch_all` — a three-hop chain.
- `tools/langchain_tools.py:web_search_tool` and
  `tools/custom_tools.py:internal_search_tool` both call `tools/shared.py:normalize_query`
  — fan-in.
- `workers/background.py` exercises nested functions and closures — the call graph
  should attribute inner-function calls to the enclosing definition, not to the
  module.

### L4 — Endpoint mapping
- `main.py` mounts `api/routes.py:router` — the extractor must follow
  `include_router` calls.
- `api/routes.py` uses `@router.get`, `@router.post`, path parameters, and
  `Depends(...)` — every decorator form we expect on FastAPI.
- `api/flask_admin.py` uses Flask's `@blueprint.route` — proves the
  `EndpointExtractor` interface actually generalizes past FastAPI.

## Import & language edge cases (deliberate)

- **Aliased imports** in `agents/coordinator_agent.py`.
- **Relative imports** throughout `api/` and `services/`.
- **`__init__.py` re-exports** in `tools/__init__.py` and `agents/__init__.py`.
- **Stacked decorators** on `workers/background.py:scheduled_cleanup`.
- **`functools.wraps` wrapper** in `utils/decorators.py`.
- **Dynamic import** in `utils/dynamic_loader.py` — the scanner is expected to
  skip this and log it as an unresolved reference. Do not "fix" this fixture.

## Using the fixture

Point the visualizer at:

- **Codebase**: `<repo>/mock-agent-project/src`
- **Database**: any local Mongo instance seeded from `mongo/dump.json` (or via
  `python mongo/seed.py --uri mongodb://localhost:27017 --db mock_agent`).

The scanner should produce roughly:
- ~7 workflow + agent + model + prompt + user + file nodes from Mongo
- ~9 tool nodes (3 per framework) with `implements` edges to code functions
- ~20+ function nodes once L3 lands
- ~7 endpoint nodes (FastAPI + Flask) once L4 lands
