/**
 * `computeView` — derives the currently-visible React Flow nodes/edges from
 * `(rawGraph, expandedIds, showOrphans)`. Extracted from `App.tsx` (task 5)
 * so it can be unit-tested directly instead of only through full-component
 * rendering — this file has no React/DOM dependency, it's a pure function
 * over plain data.
 *
 * See `App.tsx`'s module docstring for the progressive-disclosure UX rules
 * this implements. The five phases (A: aggregator discovery, B: adjacency,
 * B.5: cluster bucketing, B.6: clone materialization, C: visibility
 * propagation, D: node emission, E: edge emission) are commented inline
 * below exactly as they were in `App.tsx` — this is a relocation, not a
 * rewrite.
 */

import type { Graph, NodeType } from "@shared-types/graph";
import type { Node as RfNode, Edge as RfEdge } from "@xyflow/react";
import { EDGE_COLOR } from "./nodeStyle";
import type { EntityNodeData } from "../components/EntityNode";

/** Node types the UI ever renders as separate graph nodes. Files are
 *  excluded — they're inlined onto their parent workflow instead. If we
 *  later want to re-enable file nodes for a "show all" mode, this is the
 *  one place to change. */
const RENDERABLE_TYPES: ReadonlySet<NodeType> = new Set([
  "workflow",
  "agent",
  "model",
  "tool",
  // `file` is renderable, but only reached through an aggregator — see
  // `AGGREGATED_TYPES` below. Individual file boxes appear only after the
  // user expands the "Files (N)" aggregator that summarizes them.
  "file",
  // `function` nodes are produced by L2 (tool → code function resolution).
  // Reached only by expanding a tool.
  "function",
]);

/** Types the UI groups under an aggregator "Files (N)" node instead of
 *  rendering each child as its own box. Extending this — say to prompts —
 *  is one entry here plus a display-name row in the aggregator label.
 *
 *  Note: as of the auto-detection change, aggregators are discovered
 *  dynamically from any attribute ending in `_count`. This set is kept
 *  as a hint for the edge-based fallback (Phase A step (2) in
 *  computeView) which still needs to know "should I treat this contained
 *  child as an aggregatable type?". */
const AGGREGATED_TYPES: ReadonlySet<NodeType> = new Set(["file"]);

/** Node types that get **cloned per consumer** in the rendered graph.
 *
 * Currently: models only. Every agent that uses `gpt-4o-mini` shows its
 * own copy of the model — a shared model node ends up with many long
 * incoming edges from distant agents and the graph gets messy fast.
 *
 * Tools DELIBERATELY stay shared (not cloned): the fan-in on a shared
 * tool ("three agents all use internal_search") is architecturally
 * meaningful — it tells the reader those three agents share a code
 * path. Duplication would hide that. The per-partition layout + cross-
 * partition recentering + high-fan-in x-offset in `layout.ts` handle
 * the geometry so a shared tool with several parents still lays out
 * cleanly.
 *
 * Adding a type here re-enables Phase B.6 clone materialization for it;
 * removing it makes the node shared. That's the entire toggle. */
export const DUPLICATE_TYPES: ReadonlySet<NodeType> = new Set<NodeType>();

/** Lightweight edge shape used for the augmented adjacency below. Enough
 *  for visibility propagation and edge emission — we don't need the full
 *  GraphEdge (with provenance) here. */
type OutEdge = { sourceId: string; targetId: string; kind: string };

/** Threshold above which a source's same-(kind, target-type) children
 *  are auto-bucketed into range-labeled clusters. Chosen so a workflow
 *  with 60 agents clusters (visibly a lot) but one with 10 doesn't
 *  (still scannable). Tune here — a single number governs the trigger. */
const CLUSTER_THRESHOLD = 50;
/** Children per cluster. 100 keeps a cluster's expansion legible when the
 *  user drills in — 100 items still fits on screen at typical zoom, and
 *  1200 files fan into 12 clusters, not 24 or 4. */
const CLUSTER_BUCKET_SIZE = 100;
/** UI-synthesized cluster. Interposed between a source and a same-type,
 *  same-kind fan-out that exceeds CLUSTER_THRESHOLD. IDs are deterministic
 *  (`cluster:<parentSourceId>:<kind>:<itemType>:<bucketIndex>`) so
 *  drag positions and expansion state survive re-renders. */
interface Cluster {
  id: string;
  /** The original source whose children were bucketed. Used at edge-
   *  emission time to compute per-consumer clone ids for tools/models —
   *  so a workflow's cluster still yields `tool@agent`-style clone
   *  identities keyed on the real consumer, not on the cluster. */
  parentSourceId: string;
  /** The edge kind that was bucketed (`uses`, `delegates_to`,
   *  `contains`, ...). Preserved on the source→cluster and cluster→child
   *  edges so styling stays consistent with the un-clustered case. */
  edgeKind: string;
  /** NodeType of the bucketed children — drives cluster styling. */
  itemType: NodeType;
  /** 0-based bucket index. Used only for id stability and label ranges. */
  bucketIndex: number;
  /** The child node ids that live inside this cluster (bucket-slice). */
  childIds: string[];
  /** Precomputed human label — e.g. "Agents 1–25", "Files 101–200". */
  label: string;
}

/** UI-synthesized aggregator. One per (parent, collection) with children. */
interface Aggregator {
  id: string;              // "aggregator:<collection>:<parentId>"
  parentId: string;
  /** The Mongo collection name the aggregator's children come from
   *  (e.g. "files", "documents", "attachments"). Sent verbatim to the
   *  /graph/subtree endpoint on expand. */
  collection: string;
  /** NodeType used for visual styling (color + shape). Falls back to
   *  "file" when the collection name isn't a recognized domain type. */
  itemType: NodeType;
  /** Human-readable plural for the aggregator label ("Files", "Documents"). */
  label: string;
  /** Real node ids this aggregator stands in for. Populated after the
   *  subtree fetch merges children + edges into rawGraph. */
  childIds: string[];
  /** Backend-reported total (from the lazy-count aggregation). Only set
   *  when the parent's attributes carry a `<lazy>_count` field. Used to
   *  render the label BEFORE the subtree fetch has completed. */
  expectedCount?: number;
}

/** Agents with no incoming `uses` edge whose source is a workflow.
 *  These exist in the DB but no workflow references them, so progressive
 *  disclosure (which starts at workflows) never reveals them. Edge-based
 *  rather than "not in any `agent_ids`" so the definition survives when
 *  the L1 collection mapping changes — anything that produces a
 *  workflow→agent `uses` edge counts. */
export function computeOrphanAgentIds(raw: Graph): Set<string> {
  const workflowIds = new Set<string>();
  for (const n of raw.nodes) if (n.type === "workflow") workflowIds.add(n.id);
  const attachedAgentIds = new Set<string>();
  for (const e of raw.edges) {
    if (e.kind !== "uses") continue;
    if (!workflowIds.has(e.sourceId)) continue;
    attachedAgentIds.add(e.targetId);
  }
  const orphans = new Set<string>();
  for (const n of raw.nodes) {
    if (n.type !== "agent") continue;
    if (attachedAgentIds.has(n.id)) continue;
    orphans.add(n.id);
  }
  return orphans;
}
/** Singular collection name → NodeType. Used when discovering
 *  aggregators from `<singular>_count` attributes: a `file_count`
 *  attribute means the aggregator holds `file`-type nodes. Anything
 *  not in this map falls back to the "file" NodeType for styling — so
 *  a user's `documents` collection shows up styled like files
 *  (sky-blue rounded rect) even though we don't have a dedicated
 *  color for it. Add explicit entries here if you want distinct
 *  visuals for a new collection. */
const KNOWN_ITEM_TYPES: Record<string, NodeType> = {
  file: "file",
  prompt: "prompt",
  tool: "tool",
  model: "model",
  workflow: "workflow",
  agent: "agent",
  user: "user",
  function: "function",
  endpoint: "endpoint",
};

/** Human-friendly plural for aggregator labels. Extend when we add more
 *  aggregated types. */
const AGGREGATOR_LABEL: Partial<Record<NodeType, string>> = {
  file: "Files",
  prompt: "Prompts",
  tool: "Tools",
};
/** Compute the derived view: visible nodes (real + aggregator + model clones)
 *  + edges (real + synthetic through aggregators, source-must-be-expanded).
 *
 *  `showOrphans` seeds orphan agents into the initial visible set alongside
 *  workflows. Everything downstream (expansion propagation, aggregator
 *  discovery, model/tool duplication, edge emission) is unchanged because
 *  it's all driven off the seed set + `expandedIds`. */
export function computeView(
  raw: Graph,
  expandedIds: ReadonlySet<string>,
  showOrphans: boolean,
): {
  nodes: RfNode[];
  edges: RfEdge[];
} {
  const byId = new Map(raw.nodes.map((n) => [n.id, n]));

  // --- Phase A: Discover aggregators -----------------------------------
  // Two signals feed aggregator discovery:
  //
  //   (1) Attribute-based signal: a parent node carries an
  //       `<lazy>_count` attribute (e.g. workflow.attributes.fileCount)
  //       set by the backend during initial /graph. This tells us "there
  //       are N children out there, but they weren't fetched." The
  //       aggregator's `expectedCount` comes from here; `childIds` starts
  //       empty until the subtree fetch fires.
  //
  //   (2) Edge-based signal: after the subtree fetch merges child nodes
  //       and workflow→child `contains` edges into rawGraph, we can also
  //       enumerate the concrete children. Both signals coexist — the
  //       aggregator label uses the attribute count if present, else the
  //       edge count.
  const aggregators = new Map<string, Aggregator>();

  // (1) attribute-based (schema-agnostic): any parent node carrying an
  // attribute whose key ends in `_count`. The backend emits
  // `<lazy_singular>_count` for every collection it detected as lazy
  // (files → file_count, documents → document_count, and so on), so
  // scanning all `_count` attributes here handles all of them without
  // hardcoding collection names on the frontend.
  //
  // GOTCHA: Pydantic's `alias_generator=to_camel` only transforms model
  // fields, not keys inside `dict[str, Any]` payloads. Node.attributes is
  // one of those dicts — its keys pass through as-is. `_count` (snake)
  // stays snake on the wire. Do NOT convert to camelCase here.
  for (const n of raw.nodes) {
    const attrs = n.attributes as Record<string, unknown>;
    for (const [key, raw_value] of Object.entries(attrs)) {
      if (!key.endsWith("_count")) continue;
      const count = Number(raw_value);
      if (!Number.isFinite(count) || count <= 0) continue;
      // Derive collection / itemType / label from the attribute key.
      // `file_count`  → singular "file",  collection "files",  itemType "file",  label "Files"
      // `doc_count`   → singular "doc",   collection "docs",   itemType fallback, label "Docs"
      const singular = key.slice(0, -"_count".length);
      const collection = `${singular}s`;
      const itemType = KNOWN_ITEM_TYPES[singular] ?? "file";
      const label =
        AGGREGATOR_LABEL[itemType] ??
        singular.charAt(0).toUpperCase() + singular.slice(1) + "s";
      const id = `aggregator:${singular}:${n.id}`;
      aggregators.set(id, {
        id,
        parentId: n.id,
        collection,
        itemType,
        label,
        childIds: [],
        expectedCount: count,
      });
    }
  }

  // (2) edge-based: after a subtree fetch, contains edges to files exist.
  // For each such edge, populate the matching aggregator's childIds so
  // the aggregator's expansion shows the just-fetched children. If no
  // attribute-based signal produced the aggregator earlier, synthesize
  // one from just the edge (mostly for graphs the backend didn't tag
  // with counts).
  for (const e of raw.edges) {
    if (e.kind !== "contains") continue;
    const child = byId.get(e.targetId);
    if (!child || !AGGREGATED_TYPES.has(child.type)) continue;
    const aggId = `aggregator:${child.type}:${e.sourceId}`;
    let agg = aggregators.get(aggId);
    if (!agg) {
      agg = {
        id: aggId,
        parentId: e.sourceId,
        collection: `${child.type}s`,
        itemType: child.type,
        label: AGGREGATOR_LABEL[child.type] ?? `${child.type}s`,
        childIds: [],
      };
      aggregators.set(aggId, agg);
    }
    agg.childIds.push(e.targetId);
  }

  // --- Phase B: Build augmented adjacency ------------------------------
  // Regular edges (renderable-only) go in as-is. Contains-into-aggregated
  // edges are rewritten: parent → aggregator (once) + aggregator → child.
  const outByNode = new Map<string, OutEdge[]>();
  const push = (e: OutEdge) => {
    const list = outByNode.get(e.sourceId) ?? [];
    list.push(e);
    outByNode.set(e.sourceId, list);
  };
  for (const e of raw.edges) {
    const target = byId.get(e.targetId);
    if (!target || !RENDERABLE_TYPES.has(target.type)) continue;
    // Route contains-into-aggregated through the aggregator instead of
    // adding the direct edge. The synthetic edges are pushed below.
    if (e.kind === "contains" && AGGREGATED_TYPES.has(target.type)) continue;
    push({ sourceId: e.sourceId, targetId: e.targetId, kind: e.kind });
  }
  for (const agg of aggregators.values()) {
    // parent → aggregator (contains)
    push({ sourceId: agg.parentId, targetId: agg.id, kind: "contains" });
    // aggregator → each child (contains) — only walked when aggregator is expanded.
    for (const cId of agg.childIds) {
      push({ sourceId: agg.id, targetId: cId, kind: "contains" });
    }
  }

  // --- Phase B.5: Bucket large fan-outs --------------------------------
  // For each source, group its outgoing edges by (kind, targetType). Any
  // group with more than CLUSTER_THRESHOLD targets is bucketed into
  // clusters of at most CLUSTER_BUCKET_SIZE. The raw source→child edges
  // in that group are REPLACED with:
  //     source → cluster            (one edge per bucket)
  //     cluster → child             (one edge per child in the bucket)
  //
  // Groups at or below the threshold pass through unchanged. This runs
  // on the augmented adjacency, so it also buckets aggregator expansions
  // (Files aggregator with 1200 children → 12 clusters of 100).
  //
  // Aggregators themselves are never chosen as bucketed targets — the
  // aggregator IS a semantic grouping already. Clusters are also never
  // bucketed (they don't emit outgoing edges that overwhelm anything).
  const clusters = new Map<string, Cluster>();
  const bucketPlans: Array<{
    sourceId: string;
    keep: OutEdge[];
    newClusters: Cluster[];
    clusterOutgoing: OutEdge[];
  }> = [];
  for (const [sourceId, edgesFromSource] of outByNode) {
    // Group by "<kind>|<targetType>"; targets whose type we can't resolve
    // (unknown) fall into a "?" bucket that never clusters.
    const byGroup = new Map<string, OutEdge[]>();
    for (const e of edgesFromSource) {
      const targetType =
        byId.get(e.targetId)?.type ?? aggregators.get(e.targetId)?.itemType;
      if (!targetType) {
        const arr = byGroup.get(`${e.kind}|?`) ?? [];
        arr.push(e);
        byGroup.set(`${e.kind}|?`, arr);
        continue;
      }
      const key = `${e.kind}|${targetType}`;
      const arr = byGroup.get(key) ?? [];
      arr.push(e);
      byGroup.set(key, arr);
    }

    const keep: OutEdge[] = [];
    const newClusters: Cluster[] = [];
    const clusterOutgoing: OutEdge[] = [];
    let anyBucketed = false;

    for (const [groupKey, groupEdges] of byGroup) {
      if (groupEdges.length <= CLUSTER_THRESHOLD) {
        keep.push(...groupEdges);
        continue;
      }
      const [kind, targetTypeStr] = groupKey.split("|");
      if (!kind || !targetTypeStr || targetTypeStr === "?") {
        // Unknown or malformed group key — never cluster it.
        keep.push(...groupEdges);
        continue;
      }
      const targetType = targetTypeStr as NodeType;
      // Deterministic child order = stable cluster contents across renders.
      const sorted = [...groupEdges].sort((a, b) =>
        a.targetId < b.targetId ? -1 : a.targetId > b.targetId ? 1 : 0,
      );
      const bucketCount = Math.ceil(sorted.length / CLUSTER_BUCKET_SIZE);
      const pluralLabel =
        AGGREGATOR_LABEL[targetType] ??
        `${targetType.charAt(0).toUpperCase()}${targetType.slice(1)}s`;
      for (let b = 0; b < bucketCount; b++) {
        const start = b * CLUSTER_BUCKET_SIZE;
        const end = Math.min(start + CLUSTER_BUCKET_SIZE, sorted.length);
        const slice = sorted.slice(start, end);
        const cluster: Cluster = {
          id: `cluster:${sourceId}:${kind}:${targetType}:${b}`,
          parentSourceId: sourceId,
          edgeKind: kind,
          itemType: targetType,
          bucketIndex: b,
          childIds: slice.map((e) => e.targetId),
          label: `${pluralLabel} ${start + 1}–${end}`,
        };
        newClusters.push(cluster);
        // source → cluster (walks like any other outgoing edge; visibility
        // propagation reaches the cluster when the source is expanded).
        clusterOutgoing.push({
          sourceId,
          targetId: cluster.id,
          kind,
        });
      }
      anyBucketed = true;
    }
    if (anyBucketed) {
      bucketPlans.push({ sourceId, keep, newClusters, clusterOutgoing });
    }
  }
  // Apply plans. Mutating outByNode after iterating it is safe now that
  // we've collected every rewrite up front.
  for (const plan of bucketPlans) {
    outByNode.set(plan.sourceId, [...plan.keep, ...plan.clusterOutgoing]);
    for (const cluster of plan.newClusters) {
      clusters.set(cluster.id, cluster);
      // cluster → each child, walked only when the cluster is expanded.
      for (const childId of cluster.childIds) {
        push({ sourceId: cluster.id, targetId: childId, kind: cluster.edgeKind });
      }
    }
  }

  // --- Phase B.6: Pre-clone duplication-eligible targets -----------------
  // For every edge whose TARGET is a DUPLICATE_TYPES node (tool/model),
  // rewrite: source -> cloneId (per-consumer), and register the clone as
  // a first-class outByNode source with its outgoing edges = the
  // original's outgoing edges (source rewritten to the clone id).
  //
  // Why here (and not lazily in Phase E as before): once L2 gave tools
  // outgoing `implements` edges, tool clones started needing to be
  // expandable — and expansion / visibility propagation runs off
  // outByNode. If clones aren't in outByNode as keys, expanding a tool
  // reveals nothing. Materializing them here means Phase C, D, E all
  // treat them as normal nodes with no special cases.
  //
  // Clone id format: `<originalId>@<cloneScope>` where cloneScope is the
  // real consumer's id — clusters unwrap via `parentSourceId` so the
  // clone is scoped to the actual agent, not a UI-synthesized cluster.
  interface CloneMeta {
    cloneId: string;
    originalId: string;
    nodeType: NodeType;
    name: string;
  }
  const cloneMeta = new Map<string, CloneMeta>();

  // Two-pass to avoid order-dependence: pass 1 rewrites every edge whose
  // target is a duplicated type to point at a clone id, and enumerates
  // the clones' metadata. Pass 2 THEN materialises each clone's own
  // outgoing edges from the (now fully rewritten) originals. If we
  // materialised in pass 1 we'd sometimes copy edges to still-unrewritten
  // targets — safe for today's fixture (tools have no duplicate-typed
  // outgoing) but a fragility waiting to trip when the domain grows.
  //
  // Pass 1: rewrite source->target => source->clone, register cloneMeta.
  for (const [sourceId, edges] of Array.from(outByNode)) {
    const rewritten: OutEdge[] = [];
    for (const e of edges) {
      const targetNode = byId.get(e.targetId);
      if (
        !targetNode
        || !DUPLICATE_TYPES.has(targetNode.type)
        || aggregators.has(e.targetId)
        || clusters.has(e.targetId)
      ) {
        rewritten.push(e);
        continue;
      }
      const cloneScope = clusters.get(sourceId)?.parentSourceId ?? sourceId;
      const cloneId = `${e.targetId}@${cloneScope}`;
      rewritten.push({ sourceId, targetId: cloneId, kind: e.kind });

      if (!cloneMeta.has(cloneId)) {
        cloneMeta.set(cloneId, {
          cloneId,
          originalId: e.targetId,
          nodeType: targetNode.type,
          name: targetNode.name,
        });
      }
    }
    outByNode.set(sourceId, rewritten);
  }
  // Pass 2: materialise each clone's outgoing edges from the ORIGINAL's
  // rewritten outgoing. The originals were fully rewritten in pass 1, so
  // clone edges automatically reference other clones (not raw duplicates)
  // when the original's targets are themselves duplicate-eligible.
  for (const meta of cloneMeta.values()) {
    const originalOutgoing = outByNode.get(meta.originalId) ?? [];
    outByNode.set(
      meta.cloneId,
      originalOutgoing.map((oe) => ({ ...oe, sourceId: meta.cloneId })),
    );
  }

  // --- Phase C: Visibility propagation ---------------------------------
  // Same rules as before, but expandedIds may include aggregator ids, and
  // adjacency now routes through aggregators.
  const visible = new Set<string>();
  for (const n of raw.nodes) {
    if (n.type === "workflow") visible.add(n.id);
  }
  // Orphan agents (no incoming workflow→agent `uses` edge) are unreachable
  // from the workflow seed. When the toggle is on, seed them explicitly so
  // the user can still browse them and expand into their tools/models.
  if (showOrphans) {
    for (const id of computeOrphanAgentIds(raw)) visible.add(id);
  }
  let changed = true;
  while (changed) {
    changed = false;
    for (const id of expandedIds) {
      if (!visible.has(id)) continue;
      for (const e of outByNode.get(id) ?? []) {
        if (!visible.has(e.targetId)) {
          visible.add(e.targetId);
          changed = true;
        }
      }
    }
  }

  // Warnings from L2 (tool → code resolution) keyed by the offending
  // tool node id. L2 sets `ScanError.source_ref` to the tool node id
  // exactly so the frontend can attach warnings to specific tool boxes
  // without pattern-matching messages. Any scanner errors from other
  // scanners flow through the header's "N warnings" pill unchanged.
  const toolWarnings = new Map<string, string>();
  for (const e of raw.errors) {
    if (e.scanner !== "l2_tool") continue;
    // Only errors whose source_ref matches an actual tool node id become
    // per-node badges — L2 also emits a "codebase root not set" error
    // whose source_ref is a config string, not a node id; that one flows
    // to the header warnings pill instead.
    const target = byId.get(e.sourceRef);
    if (!target || target.type !== "tool") continue;
    // First writer wins if a tool has multiple L2 errors (rare — L2 emits
    // at most one per tool). Later messages could be joined with `\n`
    // but the tooltip stays legible with just the first.
    if (!toolWarnings.has(e.sourceRef)) {
      toolWarnings.set(e.sourceRef, e.message);
    }
  }

  // --- Phase D: Emit nodes ---------------------------------------------
  const nodesOut: RfNode[] = [];
  // (a) Raw entities except model clones.
  for (const n of raw.nodes) {
    if (!visible.has(n.id)) continue;
    if (DUPLICATE_TYPES.has(n.type)) continue;
    const outgoing = outByNode.get(n.id) ?? [];
    const data: EntityNodeData = {
      label: n.name,
      nodeType: n.type,
      expanded: expandedIds.has(n.id),
      expandable: outgoing.length > 0,
      warning: toolWarnings.get(n.id),
    };
    nodesOut.push({
      id: n.id,
      type: "entity",
      position: { x: 0, y: 0 },
      data: data as Record<string, unknown>,
    });
  }
  // (b) Aggregator nodes (visible only).
  for (const agg of aggregators.values()) {
    if (!visible.has(agg.id)) continue;
    const outgoing = outByNode.get(agg.id) ?? [];
    const displayCount = agg.expectedCount ?? agg.childIds.length;
    const data: EntityNodeData = {
      label: agg.label,
      nodeType: agg.itemType,
      expanded: expandedIds.has(agg.id),
      // Expandable if there's *something* to reveal — either concrete
      // children we have edges for, OR a positive expected count meaning
      // the subtree hasn't been fetched yet.
      expandable: outgoing.length > 0 || displayCount > 0,
      isAggregator: true,
      aggregatorCount: displayCount,
    };
    nodesOut.push({
      id: agg.id,
      type: "entity",
      position: { x: 0, y: 0 },
      data: data as Record<string, unknown>,
    });
  }
  // (c) Cluster nodes (visible only). Same visual treatment as aggregators
  //     (`isAggregator: true` reuses the dashed-border folder-icon render
  //     path in EntityNode) — the distinctive part is the label range.
  for (const cluster of clusters.values()) {
    if (!visible.has(cluster.id)) continue;
    const data: EntityNodeData = {
      label: cluster.label,
      nodeType: cluster.itemType,
      expanded: expandedIds.has(cluster.id),
      expandable: cluster.childIds.length > 0,
      isAggregator: true,
      aggregatorCount: cluster.childIds.length,
    };
    nodesOut.push({
      id: cluster.id,
      type: "entity",
      position: { x: 0, y: 0 },
      data: data as Record<string, unknown>,
    });
  }
  // (d) Clone nodes (visible only). Per-consumer copies of duplicated
  //     types (tool/model). Their `expandable` reflects whether the
  //     ORIGINAL has outgoing edges — L2 gave tools `implements` edges
  //     to function nodes, so tool clones are now expandable.
  for (const meta of cloneMeta.values()) {
    if (!visible.has(meta.cloneId)) continue;
    const cloneOutgoing = outByNode.get(meta.cloneId) ?? [];
    const data: EntityNodeData = {
      label: meta.name,
      nodeType: meta.nodeType,
      expanded: expandedIds.has(meta.cloneId),
      expandable: cloneOutgoing.length > 0,
      // L2 warnings key on the ORIGINAL tool id. Every clone of an
      // unresolved tool shows the same ⚠ badge — reuse the map lookup.
      warning: toolWarnings.get(meta.originalId),
    };
    nodesOut.push({
      id: meta.cloneId,
      type: "entity",
      position: { x: 0, y: 0 },
      data: data as Record<string, unknown>,
    });
  }

  // --- Phase E: Emit edges (source-must-be-expanded rule) -----------
  // Clones are already first-class in outByNode (Phase B.6), so no
  // clone-creation logic here — every edge whose source is visible +
  // expanded emits, regardless of whether the source is a real node, a
  // cluster, or a clone.
  const edgesOut: RfEdge[] = [];
  let edgeIdx = 0;
  for (const [sourceId, edges] of outByNode) {
    if (!visible.has(sourceId)) continue;
    if (!expandedIds.has(sourceId)) continue;
    for (const e of edges) {
      if (!visible.has(e.targetId)) continue;
      // Distinguish delegation from tool-use. When a Coordinator expands,
      // its sub-agents mix visually with its tools/models — same shape
      // rectangles, same-looking edges. Making delegates_to edges
      // dashed + animated + labeled turns "which of these are agents
      // I coordinate?" into a one-glance question.
      const isDelegates = e.kind === "delegates_to";
      edgesOut.push({
        id: `${sourceId}--${e.kind}-->${e.targetId}#${edgeIdx++}`,
        source: sourceId,
        target: e.targetId,
        type: "smoothstep",
        animated: isDelegates,
        label: isDelegates ? "delegates" : undefined,
        labelStyle: isDelegates
          ? { fill: "var(--edge-delegates_to)", fontSize: 10, fontWeight: 600 }
          : undefined,
        labelBgPadding: isDelegates ? [4, 2] : undefined,
        labelBgBorderRadius: isDelegates ? 3 : undefined,
        labelBgStyle: isDelegates ? { fill: "var(--app-bg)", fillOpacity: 0.9 } : undefined,
        style: {
          stroke: EDGE_COLOR[e.kind] ?? "#4B5563",
          strokeWidth: isDelegates ? 2 : 1.5,
          strokeDasharray: isDelegates ? "6 4" : undefined,
        },
        data: { kind: e.kind },
      });
    }
  }
  return { nodes: nodesOut, edges: edgesOut };
}
