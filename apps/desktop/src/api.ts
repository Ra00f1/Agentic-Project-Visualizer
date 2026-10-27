/**
 * HTTP client for the Python backend.
 *
 * The Tauri shell spawns the backend itself and hands this app its base
 * URL and session token (see `src-tauri/src/backend.rs` and
 * `src/lib/backend.ts`). Every request here goes through `request()`,
 * which resolves that info and attaches `Authorization: Bearer <token>`
 * when a token is present — outside Tauri (manual dev against a
 * hand-started `uvicorn`), `getBackendInfo()` may resolve a `null` token,
 * in which case the header is simply omitted.
 *
 * Each exported function still takes an optional `backend` parameter so
 * tests can supply a fixed `BackendInfo` instead of going through the
 * real Tauri-detection path.
 */

import type { CollectionMappingEntry, CollectionSchema, Graph } from "@shared-types/graph";
import type { DeadLetterEntry, MethodConfigEntry, RuntimeConfig, TraceMethodInfo } from "@shared-types/runtime";
import { getBackendInfo, type BackendInfo } from "./lib/backend";

interface FetchInit {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
  signal?: AbortSignal;
}

/** Resolves backend info (unless one was supplied for a test), builds the
 *  URL against its base, and attaches the bearer token when present.
 *  Throws `Error("backend session mismatch, restart the app")` on a 401 --
 *  per this task's own Design, a 401 here means the app's token and the
 *  running backend's token disagree, which isn't something to retry. */
async function request(
  path: string,
  { init = {}, searchParams, backend }: {
    init?: FetchInit;
    searchParams?: Record<string, string>;
    backend?: BackendInfo;
  } = {},
): Promise<Response> {
  const info = backend ?? (await getBackendInfo());
  const url = new URL(path, info.baseUrl);
  if (searchParams) {
    for (const [key, value] of Object.entries(searchParams)) url.searchParams.set(key, value);
  }

  const headers = { ...init.headers };
  if (info.token) headers.Authorization = `Bearer ${info.token}`;

  const response = await fetch(url, { ...init, headers });
  if (response.status === 401) {
    throw new Error("backend session mismatch, restart the app");
  }
  return response;
}

/** Shared non-2xx handling for every fetch* helper below: try to surface
 *  the backend's structured `detail` field, fall back to statusText. */
async function errorDetail(response: Response): Promise<string> {
  let detail = response.statusText;
  try {
    const body = (await response.json()) as { detail?: string };
    if (body.detail) detail = body.detail;
  } catch {
    // Response wasn't JSON — keep the statusText fallback.
  }
  return detail;
}

export interface FetchGraphParams {
  /** MongoDB connection URI. */
  uri: string;
  /** Target database name. */
  db: string;
  /** Subset of collections to scan. Omit or pass empty to scan all mapped
   *  collections. Unknown names are silently ignored server-side. */
  collections?: string[];
  /** Filesystem path to the target project's source. When set, L2 (tool ->
   *  code resolution) runs after L1 and the returned Graph carries
   *  `function` nodes + `implements` edges. When unset, only L1 runs and
   *  the graph carries a single advisory ScanError explaining L2 was
   *  skipped. Frontend supplies this from the Setup screen. */
  codebaseRoot?: string;
  /** Per-collection mapping the user set on the Setup screen. Omit to fall
   *  back to the backend's DEFAULT_COLLECTION_MAPPING (mock-fixture shape).
   *  Present values are keyed by collection name; every entry declares the
   *  collection's role, id field, name field, and parent-reference fields.
   *  Entries with role === "other" are dropped server-side. */
  mapping?: Record<string, CollectionMappingEntry>;
  /** Override backend resolution. Only useful in tests. */
  backend?: BackendInfo;
  /** AbortSignal so a long scan can be cancelled if the user navigates away. */
  signal?: AbortSignal;
}

export interface FetchSchemaParams {
  uri: string;
  db: string;
  backend?: BackendInfo;
  signal?: AbortSignal;
}

export interface FetchSubtreeParams {
  /** The Node.id of the parent whose children we want. */
  parentId: string;
  /** Which child collection to fetch (e.g. 'files', 'documents',
   *  'attachments'). Backend derives the foreign key by convention:
   *  `<parent_singular>_id`. */
  collection: string;
  /** MongoDB URI and DB name — must match the parent's namespace. */
  uri: string;
  db: string;
  backend?: BackendInfo;
  signal?: AbortSignal;
}

/** GET /graph/subtree → the children of one parent, on demand.
 *  Used by every lazy aggregator: initial /graph doesn't emit children of
 *  a lazy collection, so clicking the aggregator fetches them via this
 *  endpoint, and the frontend merges the result into its local rawGraph. */
export async function fetchSubtree({
  parentId,
  collection,
  uri,
  db,
  backend,
  signal,
}: FetchSubtreeParams): Promise<Graph> {
  const response = await request("/graph/subtree", {
    searchParams: { parentId, collection, uri, db },
    init: { signal },
    backend,
  });
  if (!response.ok) {
    throw new Error(`GET /graph/subtree failed (${response.status}): ${await errorDetail(response)}`);
  }
  return (await response.json()) as Graph;
}

/** GET /schema → list of collections with counts + sampled fields.
 *  Feeds the setup screen. Throws on non-2xx (same error shape as fetchGraph). */
export async function fetchSchema({ uri, db, backend, signal }: FetchSchemaParams): Promise<CollectionSchema[]> {
  const response = await request("/schema", {
    searchParams: { uri, db },
    init: { signal },
    backend,
  });
  if (!response.ok) {
    throw new Error(`GET /schema failed (${response.status}): ${await errorDetail(response)}`);
  }
  return (await response.json()) as CollectionSchema[];
}

export interface FetchDeadLetterEntriesParams {
  backend?: BackendInfo;
  signal?: AbortSignal;
}

/** GET /runtime/dead-letter → recent trace events NodeIndex couldn't resolve.
 *  Feeds the DeadLetterDrawer. No uri/db params -- the dead-letter buffer is
 *  process-global (one RuntimeChannel per backend), not scoped per DB.
 *  Same error shape as the other fetch* helpers here. */
export async function fetchDeadLetterEntries({
  backend,
  signal,
}: FetchDeadLetterEntriesParams = {}): Promise<DeadLetterEntry[]> {
  const response = await request("/runtime/dead-letter", { init: { signal }, backend });
  if (!response.ok) {
    throw new Error(`GET /runtime/dead-letter failed (${response.status}): ${await errorDetail(response)}`);
  }
  return (await response.json()) as DeadLetterEntry[];
}

/**
 * POST /graph → typed `Graph`.
 *
 * Body carries the URI, DB, selected collections, optional codebase root,
 * and the user's per-collection mapping. POST (rather than GET query
 * params) because the mapping object grows past query-string sanity
 * pretty fast — a real project with 20 collections × 4 fields each is
 * already awkward as URL args.
 *
 * The backend still accepts GET /graph for backward compatibility (used
 * only by tests today), but the app always POSTs.
 *
 * Throws on network failure or non-2xx. The error message includes the
 * status code and the backend's `detail` field when present.
 */
export async function fetchGraph({
  uri,
  db,
  collections,
  codebaseRoot,
  mapping,
  backend,
  signal,
}: FetchGraphParams): Promise<Graph> {
  const body: Record<string, unknown> = { uri, db };
  if (collections && collections.length > 0) {
    body.collections = collections;
  }
  if (codebaseRoot && codebaseRoot.trim().length > 0) {
    body.codebaseRoot = codebaseRoot.trim();
  }
  if (mapping) {
    // Strip "other"-role entries client-side too. Backend does it again
    // (belt-and-suspenders), but sending them just wastes bytes and hides
    // the user's intent in the wire log.
    const filtered: Record<string, CollectionMappingEntry> = {};
    for (const [name, entry] of Object.entries(mapping)) {
      if (entry.role !== "other") filtered[name] = entry;
    }
    if (Object.keys(filtered).length > 0) {
      body.mapping = filtered;
    }
  }

  const response = await request("/graph", {
    init: {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
    backend,
  });
  if (!response.ok) {
    throw new Error(`POST /graph failed (${response.status}): ${await errorDetail(response)}`);
  }
  return (await response.json()) as Graph;
}

// --- Runtime tracing methods (Task 22) --------------------------------

export interface FetchTraceMethodsParams {
  backend?: BackendInfo;
  signal?: AbortSignal;
}

/** GET /runtime/methods → every trace method the backend knows about, in
 *  registry order. Feeds the Setup screen's "Runtime tracing" section and
 *  `RuntimeStatusChip`'s live-vs-after-the-fact wording -- both generated
 *  entirely from this list, no method-specific frontend code. */
export async function fetchTraceMethods({
  backend,
  signal,
}: FetchTraceMethodsParams = {}): Promise<TraceMethodInfo[]> {
  const response = await request("/runtime/methods", { init: { signal }, backend });
  if (!response.ok) {
    throw new Error(`GET /runtime/methods failed (${response.status}): ${await errorDetail(response)}`);
  }
  return (await response.json()) as TraceMethodInfo[];
}

export interface FetchRuntimeConfigParams {
  backend?: BackendInfo;
  signal?: AbortSignal;
}

/** GET /runtime/config → which methods are currently configured and each
 *  source's live status. Used to seed the Setup screen's checkboxes when
 *  nothing's saved in localStorage yet (so the env-var startup default
 *  shows up as checked) — see `SetupScreen.tsx`. */
export async function fetchRuntimeConfig({
  backend,
  signal,
}: FetchRuntimeConfigParams = {}): Promise<RuntimeConfig> {
  const response = await request("/runtime/config", { init: { signal }, backend });
  if (!response.ok) {
    throw new Error(`GET /runtime/config failed (${response.status}): ${await errorDetail(response)}`);
  }
  return (await response.json()) as RuntimeConfig;
}

export interface PutRuntimeConfigParams {
  methods: MethodConfigEntry[];
  backend?: BackendInfo;
  signal?: AbortSignal;
}

/** PUT /runtime/config → replaces the WHOLE running config with `methods`
 *  (not a merge — an empty list turns every source off). Throws on a 422
 *  (unknown id, duplicate id, or a setting that fails that method's own
 *  schema) with the backend's structured `detail` in the message so the
 *  caller can show it under the right field. */
export async function putRuntimeConfig({
  methods,
  backend,
  signal,
}: PutRuntimeConfigParams): Promise<RuntimeConfig> {
  const response = await request("/runtime/config", {
    init: {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ methods }),
      signal,
    },
    backend,
  });
  if (!response.ok) {
    throw new Error(`PUT /runtime/config failed (${response.status}): ${await errorDetail(response)}`);
  }
  return (await response.json()) as RuntimeConfig;
}
