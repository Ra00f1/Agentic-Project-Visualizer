/**
 * HTTP client for the Python sidecar.
 *
 * For v1 the sidecar is not yet launched from Tauri — the developer runs
 * `uvicorn` in a separate terminal. So the base URL is a plain string here,
 * defaulting to what the backend's `Settings` also defaults to (127.0.0.1:8765).
 * When we wire the sidecar (bundled Python binary launched by the Rust shell),
 * the shell will inject the real port + session token via Tauri's
 * `invoke("get_sidecar_config")` and this file becomes the single place that
 * change lands.
 */

import type { CollectionMappingEntry, CollectionSchema, Graph } from "@shared-types/graph";

const DEFAULT_BASE_URL = "http://127.0.0.1:8765";

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
  /** Override the sidecar base URL. Only useful in tests. */
  baseUrl?: string;
  /** AbortSignal so a long scan can be cancelled if the user navigates away. */
  signal?: AbortSignal;
}

export interface FetchSchemaParams {
  uri: string;
  db: string;
  baseUrl?: string;
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
  baseUrl?: string;
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
  baseUrl = DEFAULT_BASE_URL,
  signal,
}: FetchSubtreeParams): Promise<Graph> {
  const url = new URL("/graph/subtree", baseUrl);
  url.searchParams.set("parentId", parentId);
  url.searchParams.set("collection", collection);
  url.searchParams.set("uri", uri);
  url.searchParams.set("db", db);

  const response = await fetch(url, { signal });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // Not JSON.
    }
    throw new Error(`GET /graph/subtree failed (${response.status}): ${detail}`);
  }
  return (await response.json()) as Graph;
}

/** GET /schema → list of collections with counts + sampled fields.
 *  Feeds the setup screen. Throws on non-2xx (same error shape as fetchGraph). */
export async function fetchSchema({
  uri,
  db,
  baseUrl = DEFAULT_BASE_URL,
  signal,
}: FetchSchemaParams): Promise<CollectionSchema[]> {
  const url = new URL("/schema", baseUrl);
  url.searchParams.set("uri", uri);
  url.searchParams.set("db", db);

  const response = await fetch(url, { signal });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // Not JSON.
    }
    throw new Error(`GET /schema failed (${response.status}): ${detail}`);
  }
  return (await response.json()) as CollectionSchema[];
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
  baseUrl = DEFAULT_BASE_URL,
  signal,
}: FetchGraphParams): Promise<Graph> {
  const url = new URL("/graph", baseUrl);
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

  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    // Try to surface the backend's structured detail; fall back to raw text.
    let detail = response.statusText;
    try {
      const errBody = (await response.json()) as { detail?: string };
      if (errBody.detail) detail = errBody.detail;
    } catch {
      // Response wasn't JSON — keep the statusText fallback.
    }
    throw new Error(`POST /graph failed (${response.status}): ${detail}`);
  }
  return (await response.json()) as Graph;
}
