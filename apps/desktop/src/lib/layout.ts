/**
 * Dagre auto-layout for the React Flow graph.
 *
 * CLAUDE.md §7 says: "auto-layout on first load via dagre, left-to-right
 * along the pipeline direction (workflow → agent → tool → function → endpoint)."
 * That's what `rankdir: "LR"` gives us. User-adjusted positions will override
 * this on subsequent renders — but that persistence lives in a later slice.
 *
 * Why dagre in the browser (not the backend): layout depends on the node's
 * rendered size (font, DPI, viewport). Doing it on the frontend keeps that
 * cohesion local. The backend stays fully headless.
 *
 * Multi-workflow partitioning
 * ---------------------------
 * Running one dagre pass over the whole visible graph causes rank collisions
 * between distant workflows: every workflow's rank-2 nodes land in the same
 * vertical column, so `web_search` (a coordinator's tool, 2 hops from its
 * workflow) mixes with a moderator worker (2 hops from *another* workflow).
 * Dagre has no built-in "each workflow owns its own region" primitive.
 *
 * Fix: identify visible workflow nodes, BFS their descendants (first-wins
 * assignment for nodes reachable from more than one), run a separate dagre
 * pass per partition, then stack the results vertically with a fixed gap.
 * Nodes not reachable from any workflow (orphan-agent expansions, mostly)
 * form their own trailing partition.
 *
 * Cross-partition edges (a shared function called from tools in two
 * different workflows) are kept in the output edge list — dagre never sees
 * them, so they don't influence rank placement, but React Flow still
 * renders them. They appear as long connectors between the partitions,
 * which is the honest visualization of "these workflows share this
 * function."
 */

import dagre from "@dagrejs/dagre";
import type { Node as RfNode, Edge as RfEdge } from "@xyflow/react";

/** Rough size we allot to each rendered node — must match the CSS in the
 *  custom node component. If the node visually grows past this, edges start
 *  overlapping. Update both here and there together. */
const NODE_WIDTH = 160;
const NODE_HEIGHT = 44;

/** Vertical padding between per-workflow layout regions. Large enough that
 *  the top of one workflow's tallest branch doesn't touch the bottom of
 *  the previous. Small enough that scrolling between workflows stays cheap. */
const WORKFLOW_REGION_GAP = 120;

export interface LayoutOptions {
  /** Direction — LR (left→right, our default), TB (top→bottom), etc. */
  direction?: "LR" | "TB" | "RL" | "BT";
  /** Horizontal gap between ranks. */
  rankSep?: number;
  /** Vertical gap between nodes in the same rank. */
  nodeSep?: number;
  /** Dagre rank assignment algorithm.
   *
   *  - "longest-path": every node's rank = length of the longest path to
   *    it from any source. Consistent tiers: a sub-agent reachable both
   *    directly from a workflow AND via a coordinator lands in the
   *    deeper tier because the longer path wins.
   *  - "network-simplex" (dagre default): optimises for total edge
   *    length. More compact, but pulls nodes into shallower tiers when
   *    a shorter alternative path exists.
   *  - "tight-tree": intermediate, rarely useful for our shape.
   */
  ranker?: "longest-path" | "network-simplex" | "tight-tree";
}

/**
 * Given React Flow nodes+edges, return new nodes with `position` set.
 * Edges are returned untouched — dagre lays out nodes, edges follow.
 */
export function layoutGraph(
  nodes: RfNode[],
  edges: RfEdge[],
  opts: LayoutOptions = {},
  /** Optional ordering hint: workflow IDs listed here take that spot in
   *  the vertical partition stack. Any workflow ID not in the list falls
   *  back to alphabetical order after the listed ones. Fed by App.tsx
   *  from user-drag positions so "user dragged Large-scale above Research
   *  and Write" becomes "Large-scale's partition stacks first." */
  preferredWorkflowOrder?: string[],
): { nodes: RfNode[]; edges: RfEdge[] } {
  // Partition nodes by workflow ancestor. If 0 or 1 workflow is visible
  // there's nothing to gain from partitioning — go straight to a single
  // dagre pass, same behaviour as before this restructure.
  const partitions = _partitionByWorkflow(nodes, edges);
  if (preferredWorkflowOrder && preferredWorkflowOrder.length > 0) {
    const order = new Map(preferredWorkflowOrder.map((id, i) => [id, i]));
    // Stable-sort: partitions with a preferred index come first in that
    // order; anything else keeps its existing (workflow-id) order.
    partitions.sort((a, b) => {
      const aIdx = order.has(a.workflowId) ? order.get(a.workflowId)! : Number.MAX_SAFE_INTEGER;
      const bIdx = order.has(b.workflowId) ? order.get(b.workflowId)! : Number.MAX_SAFE_INTEGER;
      return aIdx - bIdx;
    });
  }
  if (partitions.length <= 1) {
    return _dagreLayout(nodes, edges, opts);
  }

  // Multi-workflow: run dagre per partition, then vertically stack.
  const nodeById = new Map(nodes.map((n) => [n.id, n]));
  const outNodes: RfNode[] = [];
  let yOffset = 0;
  for (const partition of partitions) {
    const partNodes = partition.nodeIds
      .map((id) => nodeById.get(id))
      .filter((n): n is RfNode => n !== undefined);
    // Only edges internal to the partition affect this partition's layout.
    // Cross-partition edges (rare — e.g. a shared function called from two
    // workflows' tools) fall through to the returned edge list untouched.
    const partEdges = edges.filter(
      (e) =>
        partition.idSet.has(e.source) && partition.idSet.has(e.target),
    );
    const laid = _dagreLayout(partNodes, partEdges, opts);
    // Height of this partition = maxY + NODE_HEIGHT of its lowest node.
    let maxY = 0;
    for (const n of laid.nodes) {
      const bottom = n.position.y + NODE_HEIGHT;
      if (bottom > maxY) maxY = bottom;
      outNodes.push({
        ...n,
        position: { x: n.position.x, y: n.position.y + yOffset },
      });
    }
    yOffset += maxY + WORKFLOW_REGION_GAP;
  }

  // Recenter cross-partition shared nodes. A node reached from more than
  // one partition (typically a function that L2/L3 resolved for tools in
  // different workflows) sits wherever its "home" partition placed it —
  // the other partition's edge into it stretches across. Moving it to
  // the y-midpoint of ALL its parents (in the composed coordinate space)
  // balances the two edge lengths and pulls the shared node into the
  // natural gap between partitions.
  const nodeIdToPartition = new Map<string, string>();
  for (const p of partitions) {
    for (const id of p.nodeIds) nodeIdToPartition.set(id, p.workflowId);
  }
  const outById = new Map(outNodes.map((n) => [n.id, n]));
  // Group parent ids by target to find shared-across-partitions targets.
  const parentsByTarget = new Map<string, string[]>();
  for (const e of edges) {
    const srcPart = nodeIdToPartition.get(e.source);
    const tgtPart = nodeIdToPartition.get(e.target);
    if (!srcPart || !tgtPart) continue;
    const list = parentsByTarget.get(e.target) ?? [];
    list.push(e.source);
    parentsByTarget.set(e.target, list);
  }
  for (const [targetId, parentIds] of parentsByTarget) {
    // Only recenter when parents span multiple partitions — a node with
    // all parents in one partition already sits inside that partition's
    // layout, no cross-partition edges to balance.
    const partitionsSet = new Set(
      parentIds.map((id) => nodeIdToPartition.get(id)).filter((v): v is string => !!v),
    );
    if (partitionsSet.size < 2) continue;

    const targetNode = outById.get(targetId);
    if (!targetNode) continue;
    const parentYs = parentIds
      .map((id) => outById.get(id)?.position.y)
      .filter((y): y is number => y !== undefined);
    if (parentYs.length === 0) continue;
    const midY = parentYs.reduce((a, b) => a + b, 0) / parentYs.length;
    targetNode.position = { x: targetNode.position.x, y: midY };
  }

  // High-fan-in x-offset — Compact mode only. Nodes with many parents
  // get pushed rightward so incoming edges have room to spread. Tiered
  // mode (longest-path ranker) needs strict tier alignment, so we skip
  // the shift there — a shifted node would appear in a column between
  // tiers and break the tiered look the user picked that mode for.
  if ((opts.ranker ?? "longest-path") !== "longest-path") {
    const FANIN_THRESHOLD = 3;
    const FANIN_STEP_PX = 40;
    const FANIN_MAX_SHIFT_PX = 200;
    const parentsCountByTarget = new Map<string, number>();
    for (const [targetId, parentIds] of parentsByTarget) {
      parentsCountByTarget.set(targetId, new Set(parentIds).size);
    }
    for (const n of outNodes) {
      const fanIn = parentsCountByTarget.get(n.id) ?? 0;
      if (fanIn < FANIN_THRESHOLD) continue;
      const shift = Math.min(
        FANIN_MAX_SHIFT_PX,
        (fanIn - FANIN_THRESHOLD + 1) * FANIN_STEP_PX,
      );
      n.position = { x: n.position.x + shift, y: n.position.y };
    }
  }

  // Collision resolution — final sweep. After partition offsets, workflow
  // centering, cross-partition midpointing, and fan-in shifting, some
  // nodes end up at coincident coordinates (two shared tools called by
  // the same parents both land at the same y-midpoint, then get the
  // same x-shift for the same fan-in). Bucket by x, sort by y, and push
  // each overlapping node just below the previous with a small gap.
  //
  // Doesn't touch overall graph shape — just breaks ties between siblings
  // that ended up on top of each other.
  const X_BUCKET = 80; // treat nodes within 80px x as "same column" (matches wider rankSep)
  const MIN_Y_GAP = 24;
  const buckets = new Map<number, RfNode[]>();
  for (const n of outNodes) {
    const key = Math.round(n.position.x / X_BUCKET);
    const list = buckets.get(key) ?? [];
    list.push(n);
    buckets.set(key, list);
  }
  for (const bucket of buckets.values()) {
    if (bucket.length < 2) continue;
    bucket.sort((a, b) => a.position.y - b.position.y);
    for (let i = 1; i < bucket.length; i++) {
      const prev = bucket[i - 1]!;
      const cur = bucket[i]!;
      const minY = prev.position.y + NODE_HEIGHT + MIN_Y_GAP;
      if (cur.position.y < minY) {
        cur.position = { x: cur.position.x, y: minY };
      }
    }
  }

  // Second-pass collision resolution — CROSS-BUCKET.
  //
  // The bucket sweep above only compares nodes in the same X-bucket
  // (80px wide). Two nodes at x=460 and x=530 land in buckets 6 and 7
  // respectively — different buckets — but each is NODE_WIDTH (160)
  // wide, so their bounding boxes overlap horizontally by 90px. When
  // dagre's fan-in x-shift (above) pushes nodes into these adjacent-
  // bucket positions AND their Y ranges also happen to overlap, they
  // visually stack on top of each other. That's the "opening too many
  // nodes in Compact overlaps them" bug.
  //
  // This pass fixes it by comparing every pair (i, j) whose X-intervals
  // overlap by more than X_OVERLAP_THRESHOLD, and pushing the lower
  // one down if their Y-intervals also overlap. It is deliberately
  // Y-ONLY — we never touch X. Touching X is what caused the previous
  // regression ("interval-overlap collision" with MIN_X_SEP = NODE_WIDTH-4
  // re-narrowed the fan-in shift and grouped adjacent-rank nodes).
  //
  // Algorithm — one pass, y-sorted:
  //   1. Sort all nodes by position.y ascending.
  //   2. For each node `cur` (in Y order), look at every earlier `prev`
  //      whose Y bottom is still within reach of `cur.y + MIN_Y_GAP`.
  //   3. If `prev` also overlaps `cur` horizontally by > threshold, push
  //      `cur` down so its top is MIN_Y_GAP below `prev`'s bottom.
  //   4. Since `cur.y` only ever grows during this loop, later iterations
  //      naturally see the updated position — no re-sort needed.
  //
  // Cost is O(n²) in the worst case but tight (early-break on Y distance);
  // for the ~500-node target this runs in a few ms.
  const X_OVERLAP_THRESHOLD = 30; // ignore edge-of-edge touches; only true overlap
  const yOrdered = [...outNodes].sort((a, b) => a.position.y - b.position.y);
  for (let i = 1; i < yOrdered.length; i++) {
    const cur = yOrdered[i]!;
    let pushTo = cur.position.y;
    for (let j = i - 1; j >= 0; j--) {
      const prev = yOrdered[j]!;
      const prevBottom = prev.position.y + NODE_HEIGHT;
      // Everything above this point is too far up to collide — because
      // yOrdered is sorted, and cur's Y only grows, once we pass this
      // gate for one j we've passed for all smaller j too.
      if (prevBottom + MIN_Y_GAP <= cur.position.y) break;
      // Horizontal overlap check.
      const overlap =
        Math.min(prev.position.x + NODE_WIDTH, cur.position.x + NODE_WIDTH) -
        Math.max(prev.position.x, cur.position.x);
      if (overlap <= X_OVERLAP_THRESHOLD) continue;
      // Y collision. Take the max of every candidate — a `cur` colliding
      // with two previous nodes must clear the lower of them.
      const candidate = prevBottom + MIN_Y_GAP;
      if (candidate > pushTo) pushTo = candidate;
    }
    if (pushTo > cur.position.y) {
      cur.position = { x: cur.position.x, y: pushTo };
    }
  }

  return { nodes: outNodes, edges };
}

// ---------------------------------------------------------------------------
// Internal: single-graph dagre pass.
// ---------------------------------------------------------------------------

function _dagreLayout(
  nodes: RfNode[],
  edges: RfEdge[],
  opts: LayoutOptions = {},
): { nodes: RfNode[]; edges: RfEdge[] } {
  const {
    direction = "LR",
    rankSep = 140,
    nodeSep = 60,
    ranker = "longest-path",
  } = opts;

  // Tiered mode (user-facing name) = BFS shortest-path tiering.
  //
  // Why this replaces dagre's longest-path ranker for the Tiered mode:
  // dagre's longest-path assigns each node's rank = length of the LONGEST
  // path from any source. So an agent that has BOTH a direct edge from its
  // workflow AND an incoming `delegates_to` edge from a newly-opened
  // Coordinator sibling gets pushed to tier 3 via the longer path, even
  // though the shorter (direct) path still exists. As a result, opening
  // one coordinator visually shifted every peer agent one column right —
  // which is exactly the reason the user picked Tiered ("stable columns")
  // in the first place. BFS gives us "shortest distance from any workflow"
  // instead, which is stable under progressive disclosure.
  //
  // LR only. On the (currently unused) TB/RL/BT directions this falls
  // through to dagre, so nothing breaks if we ever wire those up.
  if (ranker === "longest-path" && direction === "LR") {
    return _tieredBfsLayout(nodes, edges, { rankSep, nodeSep });
  }

  const g = new dagre.graphlib.Graph({ multigraph: false });
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: direction, ranksep: rankSep, nodesep: nodeSep, ranker });

  // Insertion order matters: dagre uses it as a tiebreaker during the
  // vertical-placement step (its barycentric ordering algorithm). Without a
  // stable order, an aggregator node — which has no outgoing edges to
  // "anchor" it — will drift up and down as siblings appear or disappear.
  // We sort so aggregators go first (pinning them to the top of their rank),
  // then everything else by id (deterministic across renders).
  const sortedNodes = [...nodes].sort((a, b) => {
    const aIsAgg = (a.data as { isAggregator?: boolean } | undefined)?.isAggregator === true;
    const bIsAgg = (b.data as { isAggregator?: boolean } | undefined)?.isAggregator === true;
    if (aIsAgg !== bIsAgg) return aIsAgg ? -1 : 1;
    return a.id.localeCompare(b.id);
  });
  const sortedEdges = [...edges].sort((a, b) => {
    const s = a.source.localeCompare(b.source);
    return s !== 0 ? s : a.target.localeCompare(b.target);
  });

  sortedNodes.forEach((n) => g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT }));
  sortedEdges.forEach((e) => g.setEdge(e.source, e.target));

  dagre.layout(g);

  const positioned = nodes.map((n) => {
    const { x, y } = g.node(n.id);
    // dagre centers nodes; React Flow positions by top-left corner.
    return { ...n, position: { x: x - NODE_WIDTH / 2, y: y - NODE_HEIGHT / 2 } };
  });

  // Post-process: nudge workflow nodes to sit at the vertical midpoint of
  // their immediate children. Fixes the "workflow floats up top while its
  // children hang way below" look that appears when a workflow's grandchild
  // rank (e.g., 61 workers via a cluster) is much taller than its immediate
  // child rank (e.g., 2 rank-1 nodes). Dagre picks a rank position that
  // makes sense for its algorithm but the connecting edge then stretches a
  // long way vertically, which reads as a bug.
  const wfIds = new Set(
    positioned
      .filter((n) => (n.data as { nodeType?: string } | undefined)?.nodeType === "workflow")
      .map((n) => n.id),
  );
  if (wfIds.size > 0) {
    // Build one lookup for edges' source → children mapping so each
    // workflow re-uses it instead of scanning the whole edge list.
    const outByNode = new Map<string, string[]>();
    for (const e of edges) {
      const list = outByNode.get(e.source) ?? [];
      list.push(e.target);
      outByNode.set(e.source, list);
    }
    const nodePosById = new Map(positioned.map((n) => [n.id, n]));
    for (const n of positioned) {
      if (!wfIds.has(n.id)) continue;
      const childIds = outByNode.get(n.id) ?? [];
      const childYs = childIds
        .map((id) => nodePosById.get(id)?.position.y)
        .filter((y): y is number => y !== undefined);
      if (childYs.length === 0) continue;
      const midY = (Math.min(...childYs) + Math.max(...childYs)) / 2;
      n.position = { ...n.position, y: midY };
    }
  }

  return { nodes: positioned, edges };
}

// ---------------------------------------------------------------------------
// Internal: BFS shortest-path tiered layout.
//
// Used as the Tiered mode's engine, replacing dagre's longest-path ranker.
// Places every node into a column whose index equals its shortest-path
// distance from ANY visible workflow node. Nodes not reachable from any
// workflow (orphan agents and their descendants) get seeded from nodes
// with no incoming edges — the same "sources first" idea, applied locally.
//
// Column X positions are shared across the whole graph so all tier-1
// agents line up under each other regardless of which workflow they
// belong to. Y within a column is assigned by parent-average (barycenter)
// to keep edges from wandering, with aggregator-first + id tiebreakers
// for stability across renders (same reason dagre needed the same order).
// ---------------------------------------------------------------------------

function _tieredBfsLayout(
  nodes: RfNode[],
  edges: RfEdge[],
  opts: { rankSep: number; nodeSep: number },
): { nodes: RfNode[]; edges: RfEdge[] } {
  const nodeById = new Map(nodes.map((n) => [n.id, n]));

  // Build adjacency, filtering edges whose endpoints aren't visible (the
  // caller may pass a subset of the full graph — App.tsx does).
  const outByNode = new Map<string, string[]>();
  const inByNode = new Map<string, string[]>();
  for (const e of edges) {
    if (!nodeById.has(e.source) || !nodeById.has(e.target)) continue;
    const out = outByNode.get(e.source) ?? [];
    out.push(e.target);
    outByNode.set(e.source, out);
    const inn = inByNode.get(e.target) ?? [];
    inn.push(e.source);
    inByNode.set(e.target, inn);
  }

  // Multi-source BFS: every workflow node seeds tier 0 simultaneously,
  // so a node reachable from two workflows takes the shorter of the two.
  const tier = new Map<string, number>();
  const workflowIds: string[] = [];
  for (const n of nodes) {
    const nt = (n.data as { nodeType?: string } | undefined)?.nodeType;
    if (nt === "workflow") workflowIds.push(n.id);
  }
  const queue: string[] = [];
  for (const wf of workflowIds) {
    tier.set(wf, 0);
    queue.push(wf);
  }
  // Fallback for a workflow-less view (e.g. the orphan partition): seed
  // every node with no incoming edges as tier 0. This gives orphan agents
  // a sensible left-anchor without requiring a fake workflow node.
  if (queue.length === 0) {
    for (const n of nodes) {
      if ((inByNode.get(n.id) ?? []).length === 0) {
        tier.set(n.id, 0);
        queue.push(n.id);
      }
    }
  }
  while (queue.length > 0) {
    const cur = queue.shift()!;
    const curTier = tier.get(cur)!;
    for (const child of outByNode.get(cur) ?? []) {
      const existing = tier.get(child);
      const candidate = curTier + 1;
      if (existing === undefined || existing > candidate) {
        tier.set(child, candidate);
        queue.push(child);
      }
    }
  }
  // Any node still unassigned (e.g., part of a disconnected component with
  // a cycle back to a tiered node) gets tier 0 rather than being dropped.
  for (const n of nodes) {
    if (!tier.has(n.id)) tier.set(n.id, 0);
  }

  // Group by tier.
  const byTier = new Map<number, RfNode[]>();
  for (const n of nodes) {
    const t = tier.get(n.id)!;
    const list = byTier.get(t) ?? [];
    list.push(n);
    byTier.set(t, list);
  }

  const tiers = [...byTier.keys()].sort((a, b) => a - b);
  const columnX = new Map<number, number>();
  {
    let x = 0;
    for (const t of tiers) {
      columnX.set(t, x);
      x += NODE_WIDTH + opts.rankSep;
    }
  }

  const spacing = NODE_HEIGHT + opts.nodeSep;
  const positionsById = new Map<string, { x: number; y: number }>();

  // Compute each tier's total pixel height and the tallest tier's height,
  // so every column can be centered inside a shared vertical extent that
  // starts at Y=0. Centering around Y=0 directly (as an earlier version
  // did) produced negative Y values — which broke `layoutGraph`'s
  // per-partition `maxY` stacking: a partition with a 60-node fan-out
  // spanning Y=-3120..+3120 has its top half fall above the previous
  // partition, visually intermixing with it. Anchoring at Y=0 keeps every
  // partition inside `[0, maxTierHeight]` so `yOffset` stacks cleanly.
  const tierHeights = new Map<number, number>();
  let maxTierHeight = 0;
  for (const t of tiers) {
    const h = byTier.get(t)!.length * spacing;
    tierHeights.set(t, h);
    if (h > maxTierHeight) maxTierHeight = h;
  }

  for (const t of tiers) {
    const group = byTier.get(t)!;
    // Order within a tier: by average Y of already-placed parents
    // (barycentric — same principle dagre uses), with aggregator-first
    // and id tiebreakers so the ordering is deterministic across renders.
    group.sort((a, b) => {
      const aParents = inByNode.get(a.id) ?? [];
      const bParents = inByNode.get(b.id) ?? [];
      const aYs = aParents
        .map((id) => positionsById.get(id)?.y)
        .filter((y): y is number => y !== undefined);
      const bYs = bParents
        .map((id) => positionsById.get(id)?.y)
        .filter((y): y is number => y !== undefined);
      const aY = aYs.length ? aYs.reduce((s, v) => s + v, 0) / aYs.length : Number.POSITIVE_INFINITY;
      const bY = bYs.length ? bYs.reduce((s, v) => s + v, 0) / bYs.length : Number.POSITIVE_INFINITY;
      if (aY !== bY) return aY - bY;
      const aAgg = (a.data as { isAggregator?: boolean } | undefined)?.isAggregator === true;
      const bAgg = (b.data as { isAggregator?: boolean } | undefined)?.isAggregator === true;
      if (aAgg !== bAgg) return aAgg ? -1 : 1;
      return a.id.localeCompare(b.id);
    });
    const tierH = tierHeights.get(t)!;
    // Center this column within [0, maxTierHeight].
    const startY = (maxTierHeight - tierH) / 2;
    const x = columnX.get(t)!;
    group.forEach((n, i) => {
      positionsById.set(n.id, { x, y: startY + i * spacing });
    });
  }

  // Post-process: center each workflow's Y at the midpoint of its
  // immediate children. Same fix as _dagreLayout's post-process — a
  // workflow whose tier-1 children span a large Y range would otherwise
  // sit at whatever Y its (single-node) tier-0 group gave it.
  for (const wfId of workflowIds) {
    const childYs = (outByNode.get(wfId) ?? [])
      .map((id) => positionsById.get(id)?.y)
      .filter((y): y is number => y !== undefined);
    if (childYs.length === 0) continue;
    const midY = (Math.min(...childYs) + Math.max(...childYs)) / 2;
    const cur = positionsById.get(wfId);
    if (cur) positionsById.set(wfId, { x: cur.x, y: midY });
  }

  const positioned = nodes.map((n) => {
    const p = positionsById.get(n.id) ?? { x: 0, y: 0 };
    return { ...n, position: p };
  });

  return { nodes: positioned, edges };
}

// ---------------------------------------------------------------------------
// Internal: partition nodes by workflow ancestor via BFS.
// First-wins assignment: a node reachable from more than one workflow lands
// in the first workflow that BFS reaches it. That keeps a shared function
// (`normalize_query` called by tools in two workflows) inside one partition
// rather than duplicating it. The other workflow's edge to it becomes a
// cross-partition connector in the final render.
// ---------------------------------------------------------------------------

interface _Partition {
  workflowId: string;
  nodeIds: string[];
  idSet: Set<string>;
}

function _partitionByWorkflow(
  nodes: RfNode[],
  edges: RfEdge[],
): _Partition[] {
  const workflowIds: string[] = [];
  for (const n of nodes) {
    const nt = (n.data as { nodeType?: string } | undefined)?.nodeType;
    if (nt === "workflow") workflowIds.push(n.id);
  }
  if (workflowIds.length <= 1) return [];

  // Sort workflow ids so the partition order is stable across renders.
  workflowIds.sort();

  const outByNode = new Map<string, string[]>();
  for (const e of edges) {
    const list = outByNode.get(e.source) ?? [];
    list.push(e.target);
    outByNode.set(e.source, list);
  }

  const assigned = new Map<string, string>();
  for (const wfId of workflowIds) {
    // BFS from this workflow, claiming any unclaimed descendant.
    const queue: string[] = [wfId];
    while (queue.length > 0) {
      const cur = queue.shift()!;
      if (assigned.has(cur)) continue;
      assigned.set(cur, wfId);
      for (const child of outByNode.get(cur) ?? []) {
        if (!assigned.has(child)) queue.push(child);
      }
    }
  }

  // Anything still unassigned — orphan agents and their descendants that
  // no workflow reaches — becomes its own partition at the end.
  const orphanIds: string[] = [];
  for (const n of nodes) {
    if (!assigned.has(n.id)) orphanIds.push(n.id);
  }

  const partitions: _Partition[] = [];
  for (const wfId of workflowIds) {
    const ids = nodes
      .filter((n) => assigned.get(n.id) === wfId)
      .map((n) => n.id);
    partitions.push({
      workflowId: wfId,
      nodeIds: ids,
      idSet: new Set(ids),
    });
  }
  if (orphanIds.length > 0) {
    partitions.push({
      workflowId: "__orphans__",
      nodeIds: orphanIds,
      idSet: new Set(orphanIds),
    });
  }
  return partitions;
}
