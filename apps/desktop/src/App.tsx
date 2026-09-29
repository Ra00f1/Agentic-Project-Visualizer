/**
 * The graph page — progressive-disclosure vertical slice.
 *
 * UX rules driven by the current spec:
 *   1. On load, only workflow nodes are visible.
 *   2. Clicking a node toggles its "expanded" state — its outgoing neighbors
 *      (agents, models, tools, other agents via delegates_to) become visible.
 *   3. File nodes are NEVER rendered as separate boxes. Instead, files that
 *      belong to a workflow are shown as an inline list inside the workflow
 *      node itself. Backend still returns them; the frontend chooses the
 *      presentation.
 *   4. Edges are drawn only between visible nodes. Everything else is
 *      derived — no imperative graph state to keep in sync.
 *
 * Why derive on every render instead of mutating React Flow's state on
 * click: the visible set is a pure function of `(rawGraph, expandedIds)`.
 * Mutating on click would give us two sources of truth and a class of
 * "why does the ring linger after collapse" bugs. Recomputation is cheap
 * for graphs of this size.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Node as RfNode,
  type Edge as RfEdge,
  type ReactFlowInstance,
} from "@xyflow/react";
import type { Graph, GraphNode } from "@shared-types/graph";
import { fetchGraph, fetchSubtree } from "./api";
import { openUrl } from "@tauri-apps/plugin-opener";
import { layoutGraph } from "./lib/layout";
import { computeView, computeOrphanAgentIds } from "./lib/computeView";
import { useRuntimeChannel } from "./lib/runtimeChannel";
import { EntityNode, type EntityNodeData } from "./components/EntityNode";
import { OrganicEdge } from "./components/OrganicEdge";
import { LegendBar } from "./components/LegendBar";
import { NodeContextMenu } from "./components/NodeContextMenu";
import { DetailPanel } from "./components/DetailPanel";
import { DeadLetterDrawer } from "./components/DeadLetterDrawer";
import { RuntimeErrorToasts } from "./components/RuntimeErrorToasts";
import { SetupScreen } from "./components/SetupScreen";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { useTheme } from "./lib/useTheme";

const NODE_TYPES = { entity: EntityNode };
// Edge components React Flow can render. Registered once at module
// scope so the object reference stays stable across renders — passing
// a fresh object into <ReactFlow edgeTypes={...}> on every render
// forces it to re-mount every edge. Only "organic" is custom; when
// the mode is Compact or Tiered we leave the built-in "smoothstep".
const EDGE_TYPES = { organic: OrganicEdge };

export default function App() {
  // Two-phase app state. "setup" shows the connection + collection picker;
  // "graph" is everything the app used to be. Switching from graph back to
  // setup (e.g. via "Change source" button) resets the graph state so we
  // never render results from a stale connection.
  const [phase, setPhase] = useState<"setup" | "graph">("setup");
  const [uri, setUri] = useState("mongodb://localhost:27017");
  const [db, setDb] = useState("mock_agent");
  const [selectedCollections, setSelectedCollections] = useState<string[]>([]);
  // Full per-collection mapping (role + id_field + name_field +
  // parent_ref_fields) captured from the Setup screen. Passed to
  // fetchGraph so the L1 scanner can honor the user's field-name
  // overrides instead of assuming DEFAULT_COLLECTION_MAPPING's shape.
  // Empty object here means "no mapping picked yet, use backend default"
  // — matches the fetchGraph.mapping omission semantics.
  const [collectionMapping, setCollectionMapping] = useState<
    Record<string, import("@shared-types/graph").CollectionMappingEntry>
  >({});
  // Codebase root persisted in localStorage — paths are painful to retype,
  // and this is a machine-local preference (won't collide with team-shared
  // config). Falls back to empty string, which the endpoint treats as
  // "L2 not requested" and emits the advisory ScanError so the header
  // warnings pill makes the state visible.
  const [codebaseRoot, setCodebaseRoot] = useState<string>(() => {
    try {
      return localStorage.getItem("apv.codebaseRoot") ?? "";
    } catch {
      return "";
    }
  });
  const [status, setStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [rawGraph, setRawGraph] = useState<Graph | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  // Session-only toggle for "show orphan agents in the initial view."
  // Not persisted — a lightweight UI preference; matches the pattern of
  // Collapse all (session-only) rather than theme (persisted).
  const [showOrphans, setShowOrphans] = useState<boolean>(false);

  const [nodes, setNodes, onNodesChange] = useNodesState<RfNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<RfEdge>([]);

  // User-drag cache. Only holds positions the USER has explicitly moved
  // (via drag). Dagre-computed positions are NOT cached — every render
  // gets a fresh dagre pass, which is what lets new subtrees find their
  // own space and existing nodes reflow to accommodate them.
  //
  // User-dragged positions win over dagre: if the user has moved a node,
  // that position wins on every subsequent render until Refresh (or
  // Change Source) clears the cache.
  //
  // Populated by `handleNodesChange` when React Flow reports a drag-end.
  const userDragsRef = useRef<Map<string, { x: number; y: number }>>(new Map());

  // Snapshot of expandedIds captured at the start of a click, used by
  // onNodeDoubleClick to distinguish "user double-clicked a collapsed
  // node (meant to open it)" from "user double-clicked an already-open
  // node (meant to close it)". Without this, a double-click on a
  // collapsed node would expand via onClick then immediately collapse
  // via onDoubleClick — the very footgun we're fixing.
  const preClickExpandedRef = useRef<Set<string>>(new Set());

  // Esc key clears any active focus and search. Standard escape-to-cancel
  // behaviour, in one place so both features share the same convention.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setFocusNodeId(null);
        setSearchText("");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // React Flow instance — captured via onInit so App.tsx can call
  // viewport methods (setCenter, fitView) without being inside
  // ReactFlowProvider. useReactFlow() would require that wrapper.
  const rfInstanceRef = useRef<ReactFlowInstance<RfNode, RfEdge> | null>(null);

  // The last node id the user clicked to expand/collapse. After the
  // layout effect commits new positions, we pan the viewport so this
  // node lands at the current center — that preserves the user's
  // spatial context when dagre re-lays out the graph.
  const lastClickedIdRef = useRef<string | null>(null);

  // Right-click state. `contextMenu` is the transient floating menu;
  // `inspectId` is the ID of the node whose details are shown in the panel.
  // Both are strictly separate: closing the menu does NOT close the panel,
  // and vice versa.
  const [contextMenu, setContextMenu] = useState<
    { x: number; y: number; rfNodeId: string; nodeName: string } | null
  >(null);
  const [inspectId, setInspectId] = useState<string | null>(null);

  // Monotonic counter — bumped by 'Reset layout' to force a fresh dagre
  // pass even when the visible graph didn't change. useReactFlow.fitView
  // would center the view but not undo a dragged node; incrementing this
  // both invalidates the layout effect's dependency set and provides a
  // handle for future 'reset then fit' behaviour.
  const [layoutNonce, setLayoutNonce] = useState(0);

  // View mode — user's choice of layout algorithm × edge routing.
  // Persisted so a reload keeps the preferred mode; on first read
  // we migrate from the pre-Organic "apv.layoutRanker" key.
  //
  // Only three values are meaningful:
  //   compact  = dagre network-simplex + default straight-ish edges
  //   tiered   = BFS shortest-path ranker + default straight-ish edges
  //   organic  = compact layout + custom S-curve edges (OrganicEdge)
  //
  // Organic reuses compact's ranker on purpose — see comment in
  // OrganicEdge.tsx for the layout-vs-routing split.
  const [viewMode, setViewMode] = useState<"compact" | "tiered" | "organic">(
    () => {
      try {
        const v = localStorage.getItem("apv.viewMode");
        if (v === "compact" || v === "tiered" || v === "organic") return v;
        // Migration from the pre-Organic single-key state.
        const old = localStorage.getItem("apv.layoutRanker");
        return old === "network-simplex" ? "compact" : "tiered";
      } catch {
        return "tiered";
      }
    },
  );
  // Derived: which dagre ranker to hand to layoutGraph. Tiered gets
  // longest-path; both Compact and Organic get network-simplex.
  const layoutRanker: "longest-path" | "network-simplex" =
    viewMode === "tiered" ? "longest-path" : "network-simplex";

  // Click-to-focus: the node the user last clicked with intent to trace
  // (not necessarily the same as inspectId, which drives the detail
  // panel). When set, the layout effect dims every node/edge outside
  // this node's ancestor+descendant chain.
  const [focusNodeId, setFocusNodeId] = useState<string | null>(null);

  // Search: substring match on node names (case-insensitive). Highlights
  // every matching visible node (union'd with the focus chain when both
  // are active). Empty string means "no search — don't dim anything".
  const [searchText, setSearchText] = useState<string>("");

  // Aggregator ids whose subtree has already been fetched. On aggregator
  // click, if the id isn't in here, we fire /graph/subtree and merge the
  // result into rawGraph.
  const [subtreeFetched, setSubtreeFetched] = useState<Set<string>>(new Set());
  // Aggregator ids whose fetch is currently in flight — used to prevent
  // duplicate requests on rapid double-clicks and could later drive a
  // spinner on the node.
  const [subtreeLoading, setSubtreeLoading] = useState<Set<string>>(new Set());

  const { theme, toggle: toggleTheme } = useTheme();

  // Connects once, for the lifetime of the app -- deliberately not gated on
  // `phase === "graph"` (a WS connection sitting idle during the setup
  // screen costs nothing, and gating it would mean re-connecting, and
  // waiting through the reconnect backoff, every time the user returns
  // from Setup). Backend sends a snapshot on connect regardless of
  // whether a graph has ever been loaded — the runtime store just stays
  // empty until one has.
  useRuntimeChannel();

  const load = useCallback(async () => {
    setStatus("loading");
    setError(null);
    try {
      const graph = await fetchGraph({
        uri,
        db,
        // Pass empty → all collections. Non-empty → filtered set.
        collections: selectedCollections.length > 0 ? selectedCollections : undefined,
        // Empty string is treated as "no root supplied" server-side, so L2
        // is skipped and the endpoint emits its advisory ScanError.
        codebaseRoot: codebaseRoot || undefined,
        // Empty object means "user hasn't picked yet" — fetchGraph omits
        // the mapping key from the body, backend falls back to the
        // hardcoded DEFAULT_COLLECTION_MAPPING for the mock fixture.
        mapping:
          Object.keys(collectionMapping).length > 0 ? collectionMapping : undefined,
      });
      setRawGraph(graph);
      // Reset expansion on every fresh fetch — otherwise stale ids from a
      // previous DB would linger and quietly reveal nothing. Reset the
      // subtree-fetch cache too so lazy sections get re-fetched, and
      // clear cached positions so the new graph gets a fresh dagre pass
      // (old positions from a different graph shape would look weird).
      setExpandedIds(new Set());
      setSubtreeFetched(new Set());
      setSubtreeLoading(new Set());
      userDragsRef.current.clear();
      setStatus("ready");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setStatus("error");
    }
  }, [uri, db, selectedCollections, codebaseRoot, collectionMapping]);

  // Rebuild the visible React Flow graph whenever the raw graph or the
  // expanded set changes. Dagre lays it out every time — cheap at these
  // sizes and gives a clean re-layout on expand.
  // Memoize the derived view. `computeView` iterates all nodes and all
  // edges — cheap at a couple hundred nodes, less cheap after a Files
  // (1200) subtree fetch expands the raw graph. Any state change that
  // doesn't touch rawGraph or expandedIds (theme toggle, inspect click,
  // context-menu open, drag-in-progress) reuses the previous view for
  // free.
  const view = useMemo(() => {
    if (!rawGraph) return { nodes: [] as RfNode[], edges: [] as RfEdge[] };
    return computeView(rawGraph, expandedIds, showOrphans);
  }, [rawGraph, expandedIds, showOrphans]);

  // Live orphan count for the header toggle label. Cheap to recompute —
  // one pass over nodes + one pass over edges — and memoized off rawGraph
  // so it doesn't recompute on unrelated state changes (theme, inspect,
  // drag).
  const orphanCount = useMemo(
    () => (rawGraph ? computeOrphanAgentIds(rawGraph).size : 0),
    [rawGraph],
  );

  useEffect(() => {
    if (!rawGraph) return;
    // Full dagre pass every render — "dynamic" layout. This is what lets
    // the graph reflow to fit new subtrees (dagre spreads siblings apart
    // so a newly-expanded branch doesn't overlap an already-visible one).
    //
    // preferredWorkflowOrder: workflows the user has dragged carry an
    // ordering intent — if they dragged Large-scale above Research and
    // Write, Large-scale's partition should stack first. Read the workflow
    // ids sorted by their dragged y so the partitioner honours that.
    const wfIds = view.nodes
      .filter((n) => (n.data as EntityNodeData).nodeType === "workflow")
      .map((n) => n.id);
    const preferredWorkflowOrder = wfIds
      .filter((id) => userDragsRef.current.has(id))
      .sort((a, b) => {
        const ay = userDragsRef.current.get(a)!.y;
        const by = userDragsRef.current.get(b)!.y;
        return ay - by;
      });
    const laid = layoutGraph(view.nodes, view.edges, { ranker: layoutRanker }, preferredWorkflowOrder);

    const drags = userDragsRef.current;

    // Subtree translation: for every node the user has explicitly
    // dragged, compute `delta = drag_position − dagre_position`, then
    // apply that same delta to every descendant reachable from the
    // dragged node. Children follow their dragged parent instead of
    // sticking to dagre's original coordinates. If a descendant is
    // ALSO dragged, its own drag wins over the ancestor's delta.
    //
    // The nearest dragged ancestor supplies the delta — we walk BFS
    // from each dragged node and skip descendants that already have a
    // delta assigned, so nested drags stack correctly.
    const dagrePosById = new Map(
      laid.nodes.map((n) => [n.id, { x: n.position.x, y: n.position.y }]),
    );
    const outByNode = new Map<string, string[]>();
    for (const e of view.edges) {
      const list = outByNode.get(e.source) ?? [];
      list.push(e.target);
      outByNode.set(e.source, list);
    }
    // Set of workflow node ids — used to skip subtree translation on
    // workflow drags. See preferredWorkflowOrder above: a workflow drag
    // is an ORDERING signal, and the partition stack already accounts
    // for it. Propagating the drag's delta to descendants would move
    // the whole partition on top of the calculation the partitioner
    // just did, so it double-shifts and lands in an adjacent partition.
    const workflowIdSet = new Set(
      view.nodes
        .filter((n) => (n.data as EntityNodeData).nodeType === "workflow")
        .map((n) => n.id),
    );
    const deltaById = new Map<string, { dx: number; dy: number }>();
    for (const [draggedId, draggedPos] of drags) {
      if (workflowIdSet.has(draggedId)) continue;
      const dagrePos = dagrePosById.get(draggedId);
      if (!dagrePos) continue;
      const dx = draggedPos.x - dagrePos.x;
      const dy = draggedPos.y - dagrePos.y;
      // BFS descendants of the dragged node.
      const visited = new Set<string>([draggedId]);
      const queue: string[] = [draggedId];
      while (queue.length > 0) {
        const cur = queue.shift()!;
        for (const child of outByNode.get(cur) ?? []) {
          if (visited.has(child)) continue;
          visited.add(child);
          // Descendants that are themselves dragged own their delta —
          // don't overwrite. Also skip if some other dragged ancestor
          // already claimed this descendant (first BFS to arrive wins,
          // which is the nearest dragged ancestor).
          if (drags.has(child)) continue;
          if (deltaById.has(child)) continue;
          deltaById.set(child, { dx, dy });
          queue.push(child);
        }
      }
    }

    const finalNodes = laid.nodes.map((n) => {
      const drag = drags.get(n.id);
      if (drag) return { ...n, position: { x: drag.x, y: drag.y } };
      const delta = deltaById.get(n.id);
      if (delta) {
        return {
          ...n,
          position: { x: n.position.x + delta.dx, y: n.position.y + delta.dy },
        };
      }
      return n;
    });

    // Compute the highlight set — source of truth is view.nodes / view.edges
    // so we don't create a cycle with RF state below. Empty set = no dimming.
    const hlSet = new Set<string>();
    const search = searchText.trim().toLowerCase();
    if (search) {
      for (const n of view.nodes) {
        const label = (n.data as EntityNodeData).label ?? "";
        if (label.toLowerCase().includes(search)) hlSet.add(n.id);
      }
    }
    if (focusNodeId) {
      hlSet.add(focusNodeId);
      const focusOut = new Map<string, string[]>();
      const focusIn = new Map<string, string[]>();
      for (const e of view.edges) {
        const o = focusOut.get(e.source) ?? [];
        o.push(e.target);
        focusOut.set(e.source, o);
        const i = focusIn.get(e.target) ?? [];
        i.push(e.source);
        focusIn.set(e.target, i);
      }
      // BFS descendants + ancestors.
      const dq: string[] = [focusNodeId];
      while (dq.length > 0) {
        const cur = dq.shift()!;
        for (const child of focusOut.get(cur) ?? []) {
          if (!hlSet.has(child)) {
            hlSet.add(child);
            dq.push(child);
          }
        }
      }
      const aq: string[] = [focusNodeId];
      while (aq.length > 0) {
        const cur = aq.shift()!;
        for (const parent of focusIn.get(cur) ?? []) {
          if (!hlSet.has(parent)) {
            hlSet.add(parent);
            aq.push(parent);
          }
        }
      }
    }

    // Apply focus/search opacity. Empty hlSet = no dimming.
    const DIM = 0.30;
    const styledNodes = finalNodes.map((n) => {
      const isDimmed = hlSet.size > 0 && !hlSet.has(n.id);
      return { ...n, style: { ...n.style, opacity: isDimmed ? DIM : 1 } };
    });
    const styledEdges = laid.edges.map((e) => {
      const isDimmed =
        hlSet.size > 0 && !(hlSet.has(e.source) && hlSet.has(e.target));
      // Organic swaps the *type* per edge — the built-in smoothstep
      // renderer runs otherwise. This is the ONLY place viewMode
      // touches edge rendering; keeps buildView layout-agnostic.
      const type = viewMode === "organic" ? "organic" : e.type;
      return { ...e, type, style: { ...e.style, opacity: isDimmed ? DIM : 1 } };
    });
    setNodes(styledNodes);
    setEdges(styledEdges);
    // Pan to the last-clicked node's new position so the user's spatial
    // context survives dagre's re-layout. React Flow needs a frame to
    // apply the new positions before setCenter can find them — the
    // requestAnimationFrame guarantees we run after the render commit.
    const clickedId = lastClickedIdRef.current;
    if (clickedId && rfInstanceRef.current) {
      const target = finalNodes.find((n) => n.id === clickedId);
      if (target) {
        // Small width/height offset so we center on the node's midpoint
        // rather than its top-left corner. Matches layoutGraph's
        // NODE_WIDTH/NODE_HEIGHT constants — close enough for panning.
        const cx = target.position.x + 80;
        const cy = target.position.y + 22;
        const zoom = rfInstanceRef.current.getZoom();
        requestAnimationFrame(() => {
          rfInstanceRef.current?.setCenter(cx, cy, {
            zoom,
            duration: 300,
          });
        });
      }
      lastClickedIdRef.current = null;
    }
    // `view` is a memoized value derived from (rawGraph, expandedIds) — the
    // effect only needs to fire when those change, plus when React Flow's
    // setters change (they're stable, but ESLint's rule doesn't know).
  }, [rawGraph, view, setNodes, setEdges, layoutNonce, focusNodeId, searchText, layoutRanker, viewMode]);

  // Wrap React Flow's onNodesChange so we can capture the end of every
  // drag interaction and persist that position. React Flow emits many
  // change events during a drag; we only care about the one at the end
  // (dragging === false, position present) — that's the settled position.
  const handleNodesChange = useCallback(
    (changes: Parameters<typeof onNodesChange>[0]) => {
      for (const c of changes) {
        if (c.type === "position" && c.dragging === false && c.position) {
          userDragsRef.current.set(c.id, {
            x: c.position.x,
            y: c.position.y,
          });
        }
      }
      onNodesChange(changes);
    },
    [onNodesChange],
  );

  // Auto-fetch when we transition into the graph phase. The setup screen
  // gates initial load now — we don't fire /graph until the user picks a
  // connection + collection subset and hits Continue. Refresh keeps working
  // via the header button.
  useEffect(() => {
    if (phase !== "graph") return;
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  /** Merge a Graph delta (from /graph/subtree) into the local rawGraph.
   *  Dedups by node id (first-writer-wins, same as the backend merger)
   *  and by edge triple (source, target, kind). */
  const mergeSubtreeIntoGraph = useCallback((delta: Graph) => {
    setRawGraph((prev) => {
      if (!prev) return delta;
      const seenNodes = new Map(prev.nodes.map((n) => [n.id, n]));
      for (const n of delta.nodes) if (!seenNodes.has(n.id)) seenNodes.set(n.id, n);
      const seenEdges = new Map(
        prev.edges.map((e) => [`${e.sourceId}|${e.targetId}|${e.kind}`, e]),
      );
      for (const e of delta.edges) {
        const key = `${e.sourceId}|${e.targetId}|${e.kind}`;
        if (!seenEdges.has(key)) seenEdges.set(key, e);
      }
      return {
        nodes: [...seenNodes.values()],
        edges: [...seenEdges.values()],
        errors: [...prev.errors, ...delta.errors],
      };
    });
  }, []);

  const onNodeClick = useCallback(
    (_: unknown, node: RfNode) => {
      // Focus applies to every click — even leaf nodes with nothing to
      // expand. That way the user can click a function circle and see
      // which tools call it (its parents highlight, everything else dims).
      setFocusNodeId(node.id);
      const data = node.data as EntityNodeData;
      if (!data.expandable) return; // no children; nothing to expand
      // Remember this click so the layout effect can pan to it once
      // dagre has repositioned everything.
      lastClickedIdRef.current = node.id;

      // If this is an aggregator we haven't fetched yet, kick off the
      // subtree request in the background. We toggle expansion
      // optimistically — when the response arrives and merges into
      // rawGraph, the child nodes appear automatically via the
      // re-derived view.
      //
      // Aggregator ID format: `aggregator:<singular>:<parentNodeId>`
      // (parts[0] = "aggregator", parts[1] = singular collection name,
      // parts.slice(2).join(":") = the full parent node id, which may
      // itself contain colons).
      if (
        data.isAggregator &&
        node.id.startsWith("aggregator:") &&
        !subtreeFetched.has(node.id) &&
        !subtreeLoading.has(node.id)
      ) {
        const parts = node.id.split(":");
        const singular = parts[1];
        const parentId = parts.slice(2).join(":");
        if (singular && parentId) {
          const collection = `${singular}s`;
          setSubtreeLoading((prev) => new Set(prev).add(node.id));
          void fetchSubtree({ parentId, collection, uri, db })
            .then((delta) => {
              mergeSubtreeIntoGraph(delta);
              setSubtreeFetched((prev) => new Set(prev).add(node.id));
            })
            .catch((err) => {
              setError(err instanceof Error ? err.message : String(err));
            })
            .finally(() => {
              setSubtreeLoading((prev) => {
                const next = new Set(prev);
                next.delete(node.id);
                return next;
              });
            });
        }
      }

      // Snapshot expansion state BEFORE we mutate — onNodeDoubleClick
      // reads this to know if the node was already open (should collapse)
      // or was just opened by this same click (should stay open).
      preClickExpandedRef.current = new Set(expandedIds);

      setExpandedIds((prev) => {
        if (prev.has(node.id)) return prev; // already open — single-click no longer toggles
        const next = new Set(prev);
        next.add(node.id);
        return next;
      });
    },
    [uri, db, subtreeFetched, subtreeLoading, mergeSubtreeIntoGraph, expandedIds],
  );

  // Double-click collapses. Only fires the collapse when the node was
  // ALREADY expanded before this interaction — see preClickExpandedRef.
  const onNodeDoubleClick = useCallback(
    (_: unknown, node: RfNode) => {
      const data = node.data as EntityNodeData;
      if (!data.expandable) return;
      if (!preClickExpandedRef.current.has(node.id)) return; // just opened; don't undo
      lastClickedIdRef.current = node.id;
      setExpandedIds((prev) => {
        if (!prev.has(node.id)) return prev;
        const next = new Set(prev);
        next.delete(node.id);
        return next;
      });
    },
    [],
  );

  const onNodeContextMenu = useCallback(
    (event: React.MouseEvent, node: RfNode) => {
      event.preventDefault(); // suppress the browser's own context menu
      const data = node.data as EntityNodeData;
      setContextMenu({
        x: event.clientX,
        y: event.clientY,
        rfNodeId: node.id,
        nodeName: data.label,
      });
    },
    [],
  );

  /**
   * Resolve an rf-node id back to the underlying `GraphNode`.
   *
   * Model nodes are cloned per consumer with ids like `<originalId>@<consumerId>`
   * (see `computeView`). The panel shows the underlying entity's data, so
   * we strip the `@<consumerId>` suffix before the lookup. The consumer's
   * name is returned separately as `viewedVia` so the panel can hint at
   * the context ("viewed via Coordinator").
   */
  const resolveForInspect = useCallback(
    (rfNodeId: string): { node: GraphNode; viewedVia: string | null } | null => {
      if (!rawGraph) return null;
      const [originalId, consumerId] = rfNodeId.split("@") as [string, string | undefined];
      const node = rawGraph.nodes.find((n) => n.id === originalId);
      if (!node) return null;
      let viewedVia: string | null = null;
      if (consumerId) {
        const consumer = rawGraph.nodes.find((n) => n.id === consumerId);
        viewedVia = consumer?.name ?? null;
      }
      return { node, viewedVia };
    },
    [rawGraph],
  );

  /** Turn an rf-node id (real, or a `<toolId>@<consumerId>` clone) into a
   *  `vscode://file/<abs>:<line>` URI. Returns null when there's no code
   *  location we can point at — a workflow node, an unresolved tool
   *  (no implements edge), or when the user hasn't set a codebase root.
   *
   *  Composition:
   *    codebaseRoot (Windows-style OK) + "/" + node.attributes.file_path (POSIX)
   *      -> normalize backslashes to forward slashes
   *      -> encodeURI to escape spaces, keeping colons and slashes readable
   *  vscode:// accepts an absolute path suffixed with `:<line>` and jumps
   *  the editor cursor there. Cursor and Windsurf accept the same URI
   *  shape (with their own scheme prefixes) — swap the prefix in one
   *  place when we make this a user setting.
   */
  const resolveIdeUri = useCallback(
    (rfNodeId: string): string | null => {
      if (!rawGraph || !codebaseRoot) return null;
      const [originalId] = rfNodeId.split("@") as [string, string | undefined];
      // If it's already a function node, use its own attributes.
      let functionNode = rawGraph.nodes.find(
        (n) => n.id === originalId && n.type === "function",
      );
      if (!functionNode) {
        // Otherwise assume it's a tool and walk its outgoing implements edge.
        const impl = rawGraph.edges.find(
          (e) => e.sourceId === originalId && e.kind === "implements",
        );
        if (!impl) return null;
        functionNode = rawGraph.nodes.find(
          (n) => n.id === impl.targetId && n.type === "function",
        );
      }
      if (!functionNode) return null;
      const attrs = functionNode.attributes as Record<string, unknown>;
      const filePath = attrs.file_path;
      const line = attrs.line;
      if (typeof filePath !== "string" || typeof line !== "number") return null;

      // Normalize: strip trailing slash on root; always POSIX-join.
      const rootPosix = codebaseRoot.replace(/\\/g, "/").replace(/\/+$/, "");
      const abs = `${rootPosix}/${filePath}`;
      return `vscode://file/${encodeURI(abs)}:${line}`;
    },
    [rawGraph, codebaseRoot],
  );

  const inspected = inspectId ? resolveForInspect(inspectId) : null;

  const counts = useMemo(
    () =>
      rawGraph
        ? {
            visible: nodes.length,
            total: rawGraph.nodes.length,
            edges: edges.length,
            expanded: expandedIds.size,
          }
        : null,
    [rawGraph, nodes.length, edges.length, expandedIds.size],
  );

  // Setup phase renders its own screen (its own header, its own state).
  // Passing initialUri/initialDb so returning-from-graph still shows what
  // the user last used.
  if (phase === "setup") {
    return (
      <SetupScreen
        initialUri={uri}
        initialDb={db}
        initialCodebaseRoot={codebaseRoot}
        onSubmit={({ uri: newUri, db: newDb, collections, codebaseRoot: newRoot, collectionMapping: newMapping }) => {
          setUri(newUri);
          setDb(newDb);
          setSelectedCollections(collections);
          setCodebaseRoot(newRoot);
          // Full mapping now travels through to the backend on the next
          // fetchGraph call. Persist here too so a reopen picks it up
          // instantly, before the async fetchSchema finishes.
          setCollectionMapping(newMapping);
          try {
            localStorage.setItem(
              `apv.mapping.${newUri}.${newDb}`,
              JSON.stringify(newMapping),
            );
          } catch {
            // localStorage full/blocked — mapping is still live in
            // memory for this session.
          }
          try {
            localStorage.setItem("apv.codebaseRoot", newRoot);
          } catch {
            // localStorage may be full or blocked in an iframe — non-fatal,
            // the value still lives in React state for this session.
          }
          setRawGraph(null);       // drop any stale graph from a previous connection
          setExpandedIds(new Set()); // drop any stale expansion state too
          setInspectId(null);
          userDragsRef.current.clear(); // drop any user-drag positions from the old graph
          setPhase("graph");
        }}
      />
    );
  }

  // From here on, phase === "graph".
  const warningCount = rawGraph?.errors.length ?? 0;

  return (
    <div className="flex h-screen w-screen flex-col">
      <header className="flex items-center gap-3 border-b border-neutral-200 bg-white px-4 py-3 dark:border-neutral-800 dark:bg-neutral-950">
        <h1 className="mr-3 text-base font-semibold text-neutral-900 dark:text-neutral-50">
          Agentic Project Visualizer
        </h1>
        <div className="relative mr-3">
          <input
            type="text"
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            placeholder="Search nodes…"
            spellCheck={false}
            className="w-56 rounded border border-neutral-300 bg-white px-2 py-1 pr-6 text-sm text-neutral-900 placeholder:text-neutral-400 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100 dark:placeholder:text-neutral-500"
          />
          {searchText && (
            <button
              onClick={() => setSearchText("")}
              className="absolute right-1 top-1/2 -translate-y-1/2 rounded p-0.5 text-neutral-400 hover:bg-neutral-100 hover:text-neutral-700 dark:hover:bg-neutral-700 dark:hover:text-neutral-200"
              title="Clear search"
              aria-label="Clear search"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <path d="M6 6l12 12M6 18L18 6" />
              </svg>
            </button>
          )}
        </div>
        <button
          onClick={() => setPhase("setup")}
          className="rounded border border-neutral-300 px-3 py-1 text-sm text-neutral-900 hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-100 dark:hover:bg-neutral-800"
          title={`${db} on ${uri}`}
        >
          <span className="font-mono text-xs text-neutral-500 dark:text-neutral-400">DB:</span>{" "}
          <span className="font-mono text-xs">{db}</span>
          <span className="ml-2 text-xs text-neutral-500 dark:text-neutral-400">Change…</span>
        </button>
        <button
          onClick={() => void load()}
          disabled={status === "loading"}
          className="rounded bg-neutral-900 px-3 py-1 text-sm text-white disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900"
        >
          {status === "loading" ? "Loading…" : "Refresh"}
        </button>
        <button
          onClick={() => setExpandedIds(new Set())}
          disabled={expandedIds.size === 0}
          className="rounded border border-neutral-300 px-3 py-1 text-sm text-neutral-900 disabled:opacity-40 dark:border-neutral-700 dark:text-neutral-100"
          title="Collapse everything back to workflows"
        >
          Collapse all
        </button>
        <button
          onClick={() => {
            // Clear any positions the user dragged a node into, then
            // bump the layout nonce so the effect re-runs even though
            // the derived `view` reference didn't change. Combined,
            // that guarantees a fresh, drag-free dagre pass.
            userDragsRef.current.clear();
            setLayoutNonce((n) => n + 1);
          }}
          className="rounded border border-neutral-300 px-3 py-1 text-sm text-neutral-900 dark:border-neutral-700 dark:text-neutral-100"
          title="Discard any manual node positions and re-run the auto-layout from scratch"
        >
          Reset layout
        </button>
        {/* 3-way segmented toggle: Compact / Tiered / Organic.
            One rounded outer container, adjacent buttons share a
            divider. Active pill inverts to the neutral-900/100
            primary treatment used elsewhere in the toolbar. */}
        <div
          className="inline-flex overflow-hidden rounded border border-neutral-300 dark:border-neutral-700"
          role="group"
          aria-label="Layout mode"
        >
          {(["compact", "tiered", "organic"] as const).map((m, i) => {
            const on = viewMode === m;
            const label = m === "compact" ? "Compact" : m === "tiered" ? "Tiered" : "Organic";
            const explain =
              m === "compact"
                ? "Dagre picks the tightest layout."
                : m === "tiered"
                  ? "Every node lines up at its semantic tier column."
                  : "Compact layout, S-curve edges that spread parallel lines apart.";
            return (
              <button
                key={m}
                onClick={() => {
                  setViewMode(m);
                  try {
                    localStorage.setItem("apv.viewMode", m);
                  } catch {
                    // localStorage full/blocked — mode still holds
                    // for this session.
                  }
                }}
                title={explain}
                className={
                  (on
                    ? "bg-neutral-900 text-white dark:bg-neutral-100 dark:text-neutral-900"
                    : "bg-white text-neutral-700 hover:bg-neutral-50 dark:bg-neutral-950 dark:text-neutral-300 dark:hover:bg-neutral-900") +
                  (i > 0 ? " border-l border-neutral-300 dark:border-neutral-700" : "") +
                  " px-3 py-1 text-sm"
                }
              >
                {label}
              </button>
            );
          })}
        </div>
        <button
          onClick={() => setShowOrphans((v) => !v)}
          disabled={orphanCount === 0}
          className="rounded border border-neutral-300 px-3 py-1 text-sm text-neutral-900 disabled:opacity-40 dark:border-neutral-700 dark:text-neutral-100"
          title={
            orphanCount === 0
              ? "No orphan agents in this graph"
              : showOrphans
                ? "Hide agents not attached to any workflow"
                : "Show agents not attached to any workflow"
          }
        >
          {showOrphans ? "Hide orphans" : "Show orphans"}
          {orphanCount > 0 && (
            <span className="ml-1 text-neutral-500 dark:text-neutral-400">
              ({orphanCount})
            </span>
          )}
        </button>
        <div className="ml-auto flex items-center gap-3">
          {warningCount > 0 && rawGraph && (
            <span
              className="flex items-center gap-1 rounded bg-amber-100 px-2 py-1 text-xs font-medium text-amber-900 dark:bg-amber-950 dark:text-amber-300"
              title={rawGraph.errors.map((e) => `• ${e.message}`).join("\n")}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <path d="M12 9v4M12 17h.01M4.93 19h14.14a2 2 0 0 0 1.72-3L13.72 4.87a2 2 0 0 0-3.44 0L3.21 16a2 2 0 0 0 1.72 3z" />
              </svg>
              {warningCount} warning{warningCount === 1 ? "" : "s"}
            </span>
          )}
          <div className="text-xs text-neutral-500 dark:text-neutral-400">
            {status === "ready" && counts && (
              <span>
                {counts.visible}/{counts.total} nodes · {counts.edges} edges · {counts.expanded} expanded
              </span>
            )}
            {status === "error" && <span className="text-red-600 dark:text-red-400">{error}</span>}
          </div>
          <DeadLetterDrawer />
          <button
            onClick={toggleTheme}
            aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            className="rounded border border-neutral-300 p-1.5 text-neutral-700 hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800"
          >
            {theme === "dark" ? (
              // Sun icon — shown in dark mode (click to go light).
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="4" />
                <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
              </svg>
            ) : (
              // Moon icon — shown in light mode (click to go dark).
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
              </svg>
            )}
          </button>
        </div>
      </header>

      {/* Legend strip: one row of chips per node type / edge kind
          currently visible on the graph. Renders nothing before the
          first graph loads, so no empty strip on cold start. */}
      <LegendBar nodes={nodes} edges={edges} />

      {/* Flex row so opening the panel shrinks the canvas rather than
          overlapping it. React Flow watches its container size and reflows
          automatically — the minimap stays at the bottom-right of the
          (now-narrower) graph area, which is what the user wanted. */}
      <main className="flex flex-1 overflow-hidden">
        <div className="relative flex-1 min-w-0">
          {/*
           * ErrorBoundary catches render errors in the graph subtree —
           * a bad NODE_STYLE lookup, an undefined data prop, or a
           * React Flow internal glitch on a specific graph shape — and
           * shows a small in-place fallback instead of white-screening
           * the whole app.
           */}
          <ErrorBoundary>
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={NODE_TYPES}
              edgeTypes={EDGE_TYPES}
              onNodesChange={handleNodesChange}
              onEdgesChange={onEdgesChange}
              onNodeClick={onNodeClick}
              onNodeDoubleClick={onNodeDoubleClick}
              onPaneClick={() => setFocusNodeId(null)}
              onNodeContextMenu={onNodeContextMenu}
              onInit={(instance) => {
                rfInstanceRef.current = instance;
              }}
              fitView
              minZoom={0.1}
              maxZoom={2}
              proOptions={{ hideAttribution: true }}
              // React Flow ships built-in dark styles for background dots,
              // minimap, and controls — we just tell it which theme is active.
              colorMode={theme}
            >
              <Background gap={16} />
              <MiniMap zoomable pannable />
              <Controls showInteractive={false} />
            </ReactFlow>
          </ErrorBoundary>
        </div>
        {inspected && (
          <DetailPanel
            node={inspected.node}
            clonedFromContext={inspected.viewedVia}
            onClose={() => setInspectId(null)}
          />
        )}
      </main>

      {contextMenu && (() => {
        const ideUri = resolveIdeUri(contextMenu.rfNodeId);
        return (
          <NodeContextMenu
            x={contextMenu.x}
            y={contextMenu.y}
            nodeName={contextMenu.nodeName}
            onInspect={() => {
              setInspectId(contextMenu.rfNodeId);
              setContextMenu(null);
            }}
            onOpenInIde={
              ideUri
                ? () => {
                    setContextMenu(null);
                    // Fire-and-forget: openUrl hands the URI to Tauri
                    // which hands it to Windows/macOS. On first launch of
                    // a custom URL scheme (vscode://) Windows shows a
                    // one-time "allow apps to run commands?" prompt; the
                    // user can tick "always allow" to skip it.
                    void openUrl(ideUri).catch((err: unknown) => {
                      const msg = err instanceof Error ? err.message : String(err);
                      // Only surface on genuine plugin/permission rejection.
                      // eslint-disable-next-line no-alert
                      alert(`Couldn't open ${ideUri} in IDE:\n${msg}`);
                    });
                  }
                : undefined
            }
            onClose={() => setContextMenu(null)}
          />
        );
      })()}

      <RuntimeErrorToasts />
    </div>
  );
}
