# HANDOVER — Agentic Project Visualizer

You are picking up where the previous Claude left off. This document tells you what's built, what's next, who you're working with, and the decisions that are already locked so you don't relitigate them.

**Start by reading (in this order):**
1. `CLAUDE.md` at the repo root — the design contract. Sacred.
2. `README.md` — how to run the app.
3. `Agentic_Project_Visualizer_Report.docx` — full architecture write-up.
4. This file.

**The user is Raoof.** Senior developer, learning-oriented, prefers concise output, wants the *why* behind design decisions. Doesn't want dumbed-down explanations. Reads code himself. Say "risk:" when flagging risks. Skip "great question!" openings. When you disagree with him technically, say so; don't fold on real disagreements.

---

## 1. What the app is (one paragraph)

Windows-first desktop app (Mac later) that statically visualizes an agentic project. User points it at a MongoDB (Postgres/others later) that describes their agents, plus a codebase (soon). The backend scans, produces a graph of `Node` + `Edge` primitives. The React Flow frontend renders it with progressive disclosure — you start with workflows, click to reveal agents, click those to reveal tools/models. **No LLM ever runs inside the app.** It reads, parses, draws. Any suggestion to add inference triggers a scope-confirmation moment.

---

## 2. Repo layout

```
Agentic Project Visualizer/
├── apps/
│   ├── backend/          Python 3.11 · FastAPI · uv · motor · Pydantic v2
│   └── desktop/          Tauri 2 · React 19 · Vite · Tailwind v4 · React Flow · dagre
├── packages/
│   └── shared-types/     graph.ts — mirrors backend Pydantic
├── mock-agent-project/   Synthetic fixture — this IS the ground truth for scanner tests
├── CLAUDE.md             Design contract — do not violate without explicit permission
├── README.md             Setup + daily-run instructions
├── HANDOVER.md           This file
└── Agentic_Project_Visualizer_Report.docx   Full architecture report
```

---

## 3. Quick onboarding (verify env before touching anything)

Run these to confirm the previous session's state:

**Backend:**
```powershell
cd "C:\Porjects\Agentic Project Visualizer\apps\backend"
uv sync                     # install if missing
uv run pytest -v            # 19 tests, all should pass
uv run uvicorn agentic_visualizer.main:app --port 8765
```

Health check: `http://127.0.0.1:8765/health` → `{"status":"ok"}`.

**Frontend:**
```powershell
cd "C:\Porjects\Agentic Project Visualizer\apps\desktop"
npm install
npm run tauri dev
```

The Tauri window should open on the Setup screen, load 7 collections from `mock_agent`, and after Continue show the graph.

**Fixture:**
```powershell
cd "C:\Porjects\Agentic Project Visualizer\mock-agent-project"
python mongo\seed.py --drop
```

Expected: `agents upserted=11`, `files upserted=1204` (3 named + 1 named-agent-attached + 1200 generated for archive_curator).

If any of these fail, fix the environment before writing new code.

---

## 4. File map — where the important logic lives

### Backend

| File | Responsibility |
|---|---|
| `src/agentic_visualizer/main.py` | FastAPI app + endpoints: `/health`, `/schema`, `/graph`, `/graph/subtree`. Lifespan manages motor client cleanup. |
| `src/agentic_visualizer/domain/node.py` | `Node`, `Provenance`, `NodeType` literal. Frozen Pydantic models. `_DOMAIN_CONFIG` has `alias_generator=to_camel` + `populate_by_name=True`. |
| `src/agentic_visualizer/domain/edge.py` | `Edge`, `EdgeKind` literal. Deduped by `(source_id, target_id, kind)` triple. |
| `src/agentic_visualizer/domain/graph.py` | `Graph`, `ScanResult`, `ScanError`. `Graph.from_results` merges + dedups with first-writer-wins. |
| `src/agentic_visualizer/connectors/base.py` | `Connector` Protocol (structural typing, not ABC). `CollectionSchema`. |
| `src/agentic_visualizer/connectors/mongo.py` | `MongoConnector` (motor). **Module-level `_CLIENT_CACHE` keyed on `(uri, loop_id)`** — critical for tests. `_SCHEMA_CACHE` with 60s TTL. Query cap `_QUERY_MAX_TIME_MS = 10s`. |
| `src/agentic_visualizer/scanners/base.py` | `Scanner` Protocol + `ScanContext` (frozen dataclass with connector + `now()` callable for testable clocks). |
| `src/agentic_visualizer/scanners/l1_entity.py` | `L1EntityScanner`. `DEFAULT_COLLECTION_MAPPING`, `LAZY_COLLECTIONS`, `_discover_lazy_collections` (auto-detects lazy via schema pattern), `_singularize`, `_INVERTED_REFS`. |
| `src/agentic_visualizer/config.py` | `pydantic-settings`. CORS origins for Tauri. |
| `tests/` | 19 tests: 7 unit + 12 integration (require running Mongo). |

### Frontend

| File | Responsibility |
|---|---|
| `src/App.tsx` | **Main state machine + `computeView`.** Aggregator discovery, model/tool duplication, visibility propagation, edge emission with source-must-be-expanded rule, subtree fetch + merge, drag persistence with subtree translation. Memoized via `useMemo`. |
| `src/api.ts` | `fetchGraph`, `fetchSchema`, `fetchSubtree`. |
| `src/lib/layout.ts` | Dagre wrapper. Sorts nodes so aggregators come first (stability). |
| `src/lib/nodeStyle.ts` | `NODE_STYLE` map — colors/shapes per type via CSS vars. `EDGE_COLOR` per kind. |
| `src/lib/useTheme.ts` | Theme hook with localStorage persistence. |
| `src/index.css` | Tailwind v4 + `@custom-variant dark` + light/dark node palette CSS vars. |
| `src/components/EntityNode.tsx` | Custom React Flow node. Has aggregator render path (dashed border, folder icon, "Files (N)"). |
| `src/components/DetailPanel.tsx` | Right-side drawer, Inspect target. |
| `src/components/NodeContextMenu.tsx` | Right-click floating menu. |
| `src/components/SetupScreen.tsx` | First page. Collection picker with large-collection warnings, debounced auto-fetch of schema. |
| `src/components/ErrorBoundary.tsx` | Class component wrapping `<ReactFlow>`. Catches render errors, shows in-place fallback. |
| `packages/shared-types/graph.ts` | Mirrors Pydantic models (camelCase). Imported via `@shared-types/*` alias. |

---

## 5. Architecture invariants — DO NOT break these

**These are load-bearing. Get explicit permission from Raoof before changing any.**

1. **Two primitives** — Node + Edge — are the entire domain. Framework-specific shapes never leak past the scanner boundary.
2. **Node IDs are namespaced strings**: `mongo:<db>.<collection>:<oid>` for L1, `code:<file>:<line>:<qualname>` for L2/L3/L4. Deterministic across scans — that's what makes the merger sane.
3. **Provenance is required, not optional.** Every node and edge carries `(source, source_ref, scanned_at)`. Powers detail panel + refresh diffs + future jump-to-source.
4. **`Connector` and `Scanner` are `typing.Protocol`, not ABC.** No forced inheritance. Structural typing. mypy strict catches signature mismatches.
5. **camelCase on the wire, snake_case in Python.** Pydantic `alias_generator=to_camel` + `populate_by_name=True`. FastAPI `response_model_by_alias=True`.
6. **`Node.attributes` is `dict[str, Any]`.** Keys pass through Pydantic serialization UNCHANGED — no camelCase conversion inside dicts. This is a well-known gotcha. If you emit `file_count` in Python, the frontend reads `file_count`, not `fileCount`.
7. **`Graph.from_results` dedups by `(source_id, target_id, kind)` — first writer wins.** Later scanners consume upstream output; they don't mutate it.
8. **Backend is stateless-per-request.** URI + DB come from query params, never from module-level state. Two exceptions cache safely at module level: motor clients (keyed on `(uri, loop_id)`) and schema introspection (60s TTL). Both are performance-only, not correctness-relevant.
9. **No LLM inside the app.** Ever. If a task description sounds like inference, ask before writing code.
10. **The graph is read-only in v1.** All mutations happen upstream (DB or code) and land via Refresh.

---

## 6. What's built, and what's not

### Built and working (v1 slice + polish)

- Setup screen with collection picker + large-collection warnings
- `/graph` (initial scan), `/schema` (introspection), `/graph/subtree` (lazy children)
- L1 entity scanner with hardcoded + schema-auto-detected lazy collections
- Model + tool duplication per consumer (each agent gets its own copy of `gpt-4o-mini`, `internal_search`, etc.)
- Files aggregator (workflows + agents) with lazy fetch on click
- Right-click Inspect → right-side detail panel with full attributes/provenance
- Dark/light mode toggle + persistence
- Delegates_to edges visually distinct (dashed + animated + labeled)
- User-drag persistence with subtree translation (children follow dragged parent)
- ErrorBoundary around React Flow
- Backend performance: query timeouts, motor client cache (loop-aware), schema cache with TTL
- Frontend performance: `computeView` memoized

### Not built yet (roadmap)

- **L2 tool-to-function scanner** — parse `mock-agent-project/src` with `ast`, resolve tool records to code definitions, emit `implements` edges. Fixture is already set up: three tool flavors (LangChain `@tool` decorator, LlamaIndex `FunctionTool.from_defaults`, custom plain-Python), plus two distractors (`missing_impl_tool` pointing at a nonexistent function, `utils.decorators.looks_like_a_tool_but_isnt` looking tool-shaped without a Mongo row).
- **L3 call graph scanner** — extend AST walk to intra-project calls. Fixture has a 3-hop chain (`api.routes.list_documents → services.search_service.search → repositories.document_repo.default_repo.fetch_all`) and a fan-in helper (`tools.shared.normalize_query` called from both LangChain and custom flavors).
- **L4 endpoint scanner** — detect FastAPI + Flask route decorators. Fixture has both frameworks.
- **Other connectors** — Postgres, MySQL, SQLite, pgvector, Qdrant, Chroma. `Connector` Protocol is ready; each is one file.
- **Slice C: auto-cluster safety net** — when a subtree returns >50 children of same type, wrap in nested aggregators (Files 1-100, Files 101-200, etc.) instead of rendering 1200 raw boxes. This is queued as task #23 in the previous session.
- **SQLite local persistence** — user-adjusted node positions, per-graph. Currently `userDragsRef` is session-only.
- **OS keychain integration** — DB credentials via Python `keyring` + Tauri `stronghold`.
- **WebSocket streaming for scan progress** — long scans should stream stage/file/count updates.
- **Refresh triggers** — v1 is manual Refresh. `RefreshTrigger` interface for scheduled / file-watched / webhook.
- **Frontend tests** — Vitest around `computeView` would catch 90% of regressions. None currently exist.
- **Slice A/B extension: users + prompts** — currently excluded from `DEFAULT_COLLECTION_MAPPING` because they cluttered the graph. Should re-add via user-configurable node-type mapping.
- **Orphan-agents visibility** — agents not in any workflow (fact_checker, translator, data_analyst, customer_support) never surface in the current progressive-disclosure UI. Fixture has 4 of them; needs a "show orphans" toggle or a starting-set beyond just workflows.

---

## 7. Recent big decisions — do NOT relitigate

Raoof and I hashed these out. Getting these wrong will cost time.

- **Lazy files (backend never reads files collection on `/graph`).** Files carry `workflow_id` and `agent_id`. Scanner runs a single `$group` aggregation to get counts per parent, attaches `file_count` attribute. Real file docs come via `/graph/subtree` on demand.
- **Auto-detection heuristic.** A collection is auto-detected as lazy iff: (a) not hardcoded in `LAZY_COLLECTIONS`, (b) NOT referenced by any other collection (`<self_singular>_id` or `<self_singular>_ids` not present in other collections' fields), (c) HAS `<parent_singular>_id` fields pointing at other scanned collections. This prevents `agents` from being detected as lazy (they're referenced by workflow.agent_ids). See `_discover_lazy_collections` in `l1_entity.py`.
- **Attribute keys stay snake_case on the wire.** Pydantic's `alias_generator=to_camel` only applies to model fields. `Node.attributes` is `dict[str, Any]`; keys pass through. Frontend reads `attributes.file_count`, not `attributes.fileCount`. Everything on the *outside* of `attributes` is camelCase (`sourceId`, `scannedAt`, etc.).
- **Model AND tool duplication.** `DUPLICATE_TYPES = new Set(["model", "tool"])` in App.tsx. Each agent that uses a model or tool gets its own clone (id `<targetId>@<sourceId>`). Domain identity is unchanged in the raw graph — the clone is purely a UI concept, resolved back to the original in DetailPanel via `resolveForInspect`.
- **Source-must-be-expanded rule.** An edge appears only if its source is in `expandedIds`. Not just "both endpoints visible." This prevents "clicking Coordinator revealed an unrelated model clone on Research Agent" bugs.
- **Full-dagre-per-render + user drags override.** We tried position preservation and it caused stacking. We settled on: dagre lays out everything fresh every render, user-dragged nodes override dagre's positions, and dragged nodes' descendants translate by the same delta so children follow their dragged parent. See the `useEffect` in `App.tsx` — the subtree-translation via BFS is intentional, not accidental.
- **Motor client cache is `(uri, loop_id)`-keyed.** Motor's executor binds to the event loop it was constructed on. Single-loop production degenerates to per-URI keying (perf win kept). Tests each get their own loop and their own client; stale entries under a URI are lazily reaped on the next miss.
- **Two docs and one Google-style graph model** — Node/Edge. Do not introduce a third domain primitive without a compelling reason.

---

## 8. Known bugs / limitations you should know about

- **1,200-file rendering is slow.** Clicking `Files (1200)` under Archive Curator fetches all 1200 files and dagre lays them all out. Frame rate drops for a beat, then it's fine to pan. Slice C (auto-cluster) is the queued fix.
- **`_singularize` is naive.** Strips trailing `s`. Doesn't handle `people/person`, `data/datum`, etc. If Raoof's real DB has irregular plurals we'll add an override map — the `LAZY_COLLECTIONS` hardcoded config always wins over auto-detection, which is the escape hatch.
- **Orphan agents are invisible.** Progressive disclosure starts at workflows, so agents not in any workflow are unreachable. Fixture has 4 orphans deliberately. Needs a "show orphans" UI toggle.
- **MongoDB restart mid-session confuses the cached motor client.** Motor keeps trying its cached topology. Workaround: restart uvicorn. If this becomes real pain, expose an explicit "reset connection" button that calls `close_all_clients()` + `invalidate_schema_cache()`.
- **`Node.attributes` deep-copies with `_sanitize_attributes`.** For a workflow doc with a huge nested config, this is O(depth × size) per scan. Fine at fixture size, not fine at real size.
- **Manual drag doesn't persist across Refresh.** The `userDragsRef` clears on Refresh (by design — the graph shape might have changed). SQLite persistence is roadmap.

---

## 9. Gotchas — things the previous Claude learned the hard way

- **Pydantic `alias_generator` DOES NOT recurse into `dict[str, Any]`.** If you emit an attribute called `file_count` from the scanner, the wire format has `file_count` (snake), and the frontend must read `attributes.file_count`. It took an actual bug to notice this.
- **Test expectations for edge counts must distinguish pre-dedup from post-dedup.** Workflow #1 has coordinator both as `entrypoint_agent_id` and in `agent_ids[]`. Scanner emits 2 identical edges; `Graph.from_results` collapses to 1. `EXPECTED_EDGES_BY_KIND` is pre-dedup. `EXPECTED_MERGED_EDGE_COUNT = sum(...) - 1` in `test_l1_scanner.py` accounts for it.
- **Node.attributes is frozen (via `frozen=True` on `_DOMAIN_CONFIG`)**. To add an attribute to an existing node, use `n.model_copy(update={"attributes": new_dict})`. The L1 enrichment loop does this.
- **React Flow re-renders on any state change.** Wrap expensive derivations in `useMemo`. `computeView` is already memoized; if you add a new expensive derivation, memoize it.
- **The `type: ignore[import-untyped]` on motor import is intentional.** motor doesn't ship type stubs. `mypy strict` needs the ignore.
- **`_discover_lazy_collections` has the too-permissive bug's history.** Do not remove the "referenced-by-others" guard. It's what stops `agents` from being marked lazy just because agents have `model_id`.
- **CSS custom properties power the whole theme.** `nodeStyle.ts` uses `var(--node-workflow-bg)` etc. To tune a color, edit `index.css`, not the TS. When adding a new node type, add both light AND dark tokens.
- **`aggregator:<singular>:<parentId>` id format.** The parent node id can itself contain colons (`mongo:mock_agent.agents:...`), so aggregator id parsing in `onNodeClick` uses `parts.slice(2).join(":")` for the parent portion.

---

## 10. How Raoof works and what he likes

- **Concise responses.** He said so in his preferences. Skip filler, skip "I'll do X now" — just do it.
- **Explain the *why*.** Especially design decisions. Not verbose tutorials — one or two sentences of intent per non-obvious choice.
- **He reads code.** Show diffs, cite `file.py:42`, don't dump whole files unless asked.
- **When you disagree technically, say so.** He explicitly asked for that. Don't fold.
- **Ask before doing anything irreversible.** Deleting files, dropping tables, force-pushing branches.
- **He iterates fast.** He'll paste screenshots and say "this is wrong." Diagnose first, don't just try a random fix.
- **Small vs big rule:** trivial edits (<30 lines, one file) — just do them. Anything bigger — propose a plan first, wait for approval.
- **When he says "I'm still seeing X"**, don't repeat what you already told him. Re-check the actual state (read the file, run the check).

---

## 11. Fixture summary — memorize this

`mock-agent-project/mongo/dump.json` seeds 7 collections, plus `seed.py` generates 1200 bulk files.

- **users**: 2 (alice, bob) — currently EXCLUDED from `DEFAULT_COLLECTION_MAPPING`.
- **models**: 3 (gpt-4o-mini, claude-sonnet-4, llama-3-70b-instruct).
- **prompts**: 3 — currently EXCLUDED from mapping.
- **tools**: 10 (3 langchain + 3 llamaindex + 3 custom + `missing_impl_tool` distractor).
- **agents**: 11 (3 original: research/writer/coordinator; 4 workflow-members: code_reviewer/summarizer/archive_curator/content_moderator; 4 orphans: fact_checker/translator/data_analyst/customer_support).
- **workflows**: 2 (research_and_write with 7 agents + 2 models; nightly_cleanup with 0 agents + 1 model — the "no coordinator, direct LLM" case).
- **files**: 3 named workflow-attached + 1 named agent-attached (`glossary_es.md` under translator) + 1200 generated (all attached to archive_curator).

ObjectId prefix convention for readability: 0*=users, 1*=models, 2*=prompts, 3*=tools, 4*=agents, 5*=workflows, 6*=named files, 9*=generated files.

Test totals (post-dedup):
- 26 nodes (3 models + 10 tools + 11 agents + 2 workflows; files lazy)
- 48 edges (47 uses - 1 coordinator dup + 2 delegates_to)

---

## 12. First moves for the next session

Suggested sequence when you resume:

1. Run `uv run pytest -v` in `apps/backend/`. Confirm 19/19 pass.
2. Start uvicorn + Tauri, click through Setup → Refresh → expand a workflow → expand an agent → right-click Inspect one thing → toggle theme. If any of that misbehaves, that's the first bug to hunt.
3. Read `HANDOVER.md` (this file) and the "Recent big decisions" section of the report DOCX. Get the current mental model.
4. Ask Raoof what he wants next. Most likely candidates: Slice C (auto-cluster), starting on L2, adding orphan-agent visibility, or a new UI feature he mentions in the moment.
5. If the answer is "L2," open `mock-agent-project/README.md` — the fixture is heavily documented with what each L2 case exercises.

Good luck.

— Previous Claude
