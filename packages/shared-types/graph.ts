/**
 * Shared graph types — mirror the Pydantic models in apps/backend.
 *
 * The backend serializes with `alias_generator=to_camel`, so every field on
 * the wire is camelCase; these types match that shape exactly. When we add
 * `datamodel-code-generator` later, this file becomes the generator's output
 * target — the hand-rolled version here is what shipping looks like on
 * day one, no more.
 *
 * Do NOT add any transformation logic in this file. It's types-only, so it
 * can be imported into the desktop app AND (eventually) into a Node CLI or
 * test tooling without dragging runtime code along.
 */

/** Every node type the visualizer can render. Mirror of `NodeType` in domain/node.py. */
export type NodeType =
  | "workflow"
  | "agent"
  | "model"
  | "tool"
  | "prompt"
  | "user"
  | "file"
  | "function"
  | "endpoint";

/** Every relationship kind. Mirror of `EdgeKind` in domain/edge.py. */
export type EdgeKind =
  | "uses"
  | "owns"
  | "contains"
  | "delegates_to"
  | "implements"
  | "calls"
  | "exposes";

/** Where a node/edge came from — powers the detail panel and refresh diffs. */
export interface Provenance {
  source: string;
  sourceRef: string;
  /** ISO-8601 string. Kept as string here rather than `Date` so JSON round-trips
   *  are lossless — parsing to Date happens at the UI boundary, if ever. */
  scannedAt: string;
}

export interface GraphNode {
  id: string;
  type: NodeType;
  name: string;
  provenance: Provenance;
  attributes: Record<string, unknown>;
}

export interface GraphEdge {
  sourceId: string;
  targetId: string;
  kind: EdgeKind;
  provenance: Provenance;
  attributes: Record<string, unknown>;
}

export interface ScanError {
  scanner: string;
  sourceRef: string;
  message: string;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  errors: ScanError[];
}

/** Mirror of `CollectionSchema` in the backend's `connectors/base.py`.
 *  Returned by `GET /schema` and consumed by the setup screen. */
export interface CollectionSchema {
  name: string;
  /** null when the connector can't cheaply estimate a count. */
  documentCount: number | null;
  fieldNames: string[];
}

/** The role a collection plays in the visualizer's node vocabulary.
 *  Union of every NodeType the L1 scanner can produce, plus "other" — the
 *  latter marks a collection the user explicitly wants excluded. */
export type CollectionRole =
  | "workflow"
  | "agent"
  | "tool"
  | "model"
  | "prompt"
  | "user"
  | "file"
  | "other";

/** One collection's user-supplied mapping. Everything a real project needs
 *  to override about a collection's schema shape lives here.
 *
 *  Sent to the backend as part of the POST /graph body. When absent, the
 *  backend falls back to the DEFAULT_COLLECTION_MAPPING baked into
 *  l1_entity.py.
 */
export interface CollectionMappingEntry {
  /** Node type this collection produces. "other" excludes it from the graph. */
  role: CollectionRole;
  /** Doc field holding the primary key. "_id" for MongoDB by default. */
  idField: string;
  /** Doc field holding the human-readable name. */
  nameField: string;
  /** Doc field names that hold references to OTHER collections' ids —
   *  `agent_ids`, `model_id`, `tool_ids`, etc. Backend infers the target
   *  collection from each field name via the `<X>_id → collection whose role
   *  singular is X` convention, and picks the edge kind by heuristic
   *  (delegates_to for names containing "sub_" or "delegate", contains for
   *  file→workflow, uses for everything else). */
  parentRefFields: string[];
}
