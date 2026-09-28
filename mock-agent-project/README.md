# mock-agent-project

A **synthetic agentic project** used as the golden fixture for the Agentic Project
Visualizer. Nothing here is meant to *run*. Every file is a stub whose only job is
to exercise a specific shape the visualizer's scanners must handle.

Treat this as the "controlled environment" for the pipeline described in
`../CLAUDE.md` (L1 entity ingestion → L2 tool-to-function resolution → L3 call
graph → L4 endpoint mapping). When we add a new scanner rule, we add or extend a
fixture here first, then write the scanner test against it.

## Current scale (2026-09-05 expansion)

The fixture is intentionally large so the visualizer's rendering, layout, and
detail-panel paths get stressed by realistic-shaped data. Seed counts:

| Collection | Count | Purpose |
|---|---|---|
| users | 10 | Attribution (`created_by`, `owner_id`) |
| models | 8 | Includes one with a null `cost` block (edge case) |
| prompts | 28 | Various versions and variable sets; one with a null `content`; one with `version=0` |
| tools | 78 | 22-tool fanout module + shared audit log + 30 domain tools + 4 unresolved + edge cases |
| agents | 72 | Coordinators + specialists + deep-chain (6-level) + cycles + unicode + long names |
| workflows | 21 | 10 domain workflows + 8 edge-case showcases + one empty + shared-agent pair |
| files | 36 | Sizes from 0 bytes to 52 MB; one with null mime_type; unicode filenames; duplicate names |

## Layout

```
mock-agent-project/
├── src/                          Mock Python codebase (~80 files)
│   ├── main.py                   FastAPI app entrypoint (L4)
│   ├── api/
│   │   ├── routes.py             APIRouter with @get/@post, path params, Depends (L4)
│   │   ├── flask_admin.py        Flask blueprint (L4 — different framework)
│   │   └── versioned_routes.py   v1/v2 prefix routers + Depends chains (L4 stress)
│   ├── agents/
│   │   ├── research_agent.py     LangChain-style tool wrapping
│   │   ├── writer_agent.py       LlamaIndex-style tool wrapping
│   │   ├── coordinator_agent.py  Class-methods; aliased imports (L1, L3)
│   │   ├── deep_chain_agent.py   6 classes forming a delegation chain (L1)
│   │   ├── cyclic_agents.py      A ↔ B delegation cycle (L1 recursion guard)
│   │   ├── self_delegating.py    Self-loop delegation (L1 recursion guard)
│   │   ├── wide_fanout_agent.py  Class for the 22-tool fanout agent
│   │   ├── long_name.py          Class backing the 130-char-name agent
│   │   ├── unicode_agent.py      Class backing the Japanese-katakana agent
│   │   ├── verbose_agent.py      Class backing the 30-sentence description agent
│   │   ├── broken_ref.py         Classes with broken model_id / tool_id FKs
│   │   ├── no_desc.py            Agent with null description
│   │   ├── dup.py                Two agents sharing a `name`
│   │   ├── orphan.py             Agent not attached to any workflow
│   │   ├── shared_agent.py       Agent referenced by two workflows
│   │   ├── throwaway.py          Filler for the long-name workflow
│   │   └── <domain>_agents.py    10 modules × 3-5 classes for domain workflows
│   ├── tools/
│   │   ├── langchain_tools.py    @tool decorator (LangChain adapter)
│   │   ├── llamaindex_tools.py   FunctionTool.from_defaults (LlamaIndex adapter)
│   │   ├── custom_tools.py       Plain-python tools (default adapter)
│   │   ├── shared.py             Fan-in target for L3
│   │   ├── fanout_tools.py       22 sibling functions (wide fanout)
│   │   ├── shared_logging.py     `audit_log` — wide fan-in target
│   │   ├── async_tools.py        `async def` tools
│   │   ├── class_tools.py        @classmethod + @staticmethod tools
│   │   ├── lambda_tools.py       Lambda-defined tools (assignment, not def)
│   │   ├── namespace_a.py        `process` function
│   │   ├── namespace_b.py        `process` function (name collision test)
│   │   ├── reexport_pkg/
│   │   │   ├── __init__.py       Re-exports `greet` from _impl (L2 follow-through)
│   │   │   └── _impl.py          Real `greet` definition
│   │   └── <domain>_tools.py     10 domain modules × 2-4 tool functions
│   ├── services/
│   │   ├── search_service.py     Sync service (L3)
│   │   ├── llm_service.py        Async service using TaskGroup (L3)
│   │   ├── deep_call_chain.py    hop1 → hop2 → … → hop8 (L3 depth)
│   │   ├── circular_a.py         Circular import half 1
│   │   ├── circular_b.py         Circular import half 2 (import-time deferred)
│   │   └── large_module.py       40 functions in one file (L3 throughput)
│   ├── repositories/
│   │   └── document_repo.py      End of the handler → service → repo chain
│   ├── models/
│   │   └── schemas.py            Pydantic models used by the API (L4)
│   ├── utils/
│   │   ├── decorators.py         Custom decorator + functools.wraps + looks-like-tool decoy
│   │   ├── dynamic_loader.py     `__import__` — KNOWN scanner limitation
│   │   ├── wildcard_helpers.py   __all__-limited wildcard-import target
│   │   ├── wildcard_user.py      Uses `from ... import *`
│   │   ├── dispatch.py           Runtime dispatch (dict registry, getattr)
│   │   └── generators.py         yield-based functions
│   └── workers/
│       └── background.py         Nested functions, closures, stacked decorators
└── mongo/
    ├── dump.json                 Raw seed data (253 docs — MongoDB Extended JSON v2)
    └── seed.py                   Idempotent motor-based seeder
```

## Edge cases the expanded fixture covers

### Graph shape
- **Cycles**: 2-cycle (`cyclic_agent_a ↔ cyclic_agent_b`) + self-loop (`self_delegating_agent`). Progressive-disclosure BFS must not infinite-loop.
- **Deep chain**: `deep_chain_L1 → L2 → L3 → L4 → L5 → L6`. Layout must survive 6-level cascades without partitions colliding.
- **Wide fan-out**: `wide_fanout_orchestrator` uses 22 tools. Compact layout should spread them; Organic edges should stagger.
- **Wide fan-in**: `shared_audit_log` is used by 20+ agents.
- **Shared node across workflows**: `shared_across_two_workflows` appears in two workflow partitions — cross-partition edge test.
- **Orphan agent**: `orphan_agent_not_in_any_workflow` is not in any workflow's `agent_ids`. Should surface in the orphan-count pill.
- **Empty workflow**: `empty_workflow` has `agent_ids: []`. Workflow node should render alone.
- **Diamond patterns**: coordinators delegate to specialists that share domain tools.

### Broken references (L1 unresolved-edge tests)
- `broken_model_ref_agent.model_id` points at `199999...` (non-existent). L1 should surface as unresolved.
- `broken_tool_ref_agent.tool_ids` includes a non-existent tool id.
- 4 tools (`missing_impl_alpha/beta/gamma/delta`) point at `src.tools.does_not_exist` — L2 unresolved without crash.

### Missing / null / weird metadata
- Model with null `cost`.
- Prompt with null `content`.
- Prompt with `version: 0`.
- Agent with null `description` and null `system_prompt_id`.
- Tool with null `framework`.
- File with null `mime_type`.
- Two agents sharing the same `name` (different `_id`).
- Two files sharing the same `name` (different workflows).

### Label rendering
- Agent name 130 characters long (`an_extremely_long_agent_name_that_...`).
- Workflow name 76 characters long.
- Unicode agent name (Japanese katakana).
- Unicode filename (Cyrillic).
- Verbose 30-sentence description (detail panel must scroll).

### File sizes
- 0-byte file.
- 45 MB and 52 MB tarballs (won't be inspected but should render as file nodes).

### Code-side (L2/L3 targets)
- Class-based tools (`ClassTool.load`, `ClassTool.parse_static`).
- Lambda-defined tool (`add_one = lambda x: x + 1`) — AST is `Assign`, not `FunctionDef`.
- Async-def tools.
- Re-exported tool: Mongo `module_path` = package `__init__.py`, real def in `_impl.py`.
- Two modules defining `process` (namespace collision — must disambiguate by module_path).
- Circular imports between `circular_a` and `circular_b`.
- Wildcard import (`from utils.wildcard_helpers import *`) with `__all__` gating.
- Runtime dispatch via registry (`dispatch.py`) — L3 cannot statically resolve.
- 8-hop call chain (`deep_call_chain.py`).
- Single module with 40 functions (`large_module.py`) — throughput test.

## Using the fixture

Point the visualizer at:

- **Codebase**: `<repo>/mock-agent-project/src`
- **Database**: any local Mongo instance seeded from `mongo/dump.json`. Reseed with:
  ```
  python mongo/seed.py --uri mongodb://localhost:27017 --db mock_agent --drop
  ```
  The `--drop` matters — the fixture grew, so a stale DB from before this
  expansion needs a full reset.

### Regenerating the fixture

The current dump.json was built by `/tmp/expand_fixture.py` — a one-shot Python
script that (a) preserves the original 40 hand-written docs and (b) appends the
expansion listed above. If you need to regenerate or extend, save that script
alongside this README rather than editing dump.json by hand for the additions.
