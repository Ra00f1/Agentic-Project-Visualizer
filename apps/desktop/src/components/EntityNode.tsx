/**
 * The one and only custom React Flow node component (for v1).
 *
 * Per CLAUDE.md §7 — the *body* of a node normally shows only its name. Type
 * is encoded by color and shape (see nodeStyle.ts).
 *
 * Two deliberate exceptions in this iteration:
 *
 *   1. **Workflow nodes** carry an inline list of the files they contain.
 *      We do this because files-as-separate-nodes would clutter the graph
 *      with leaf boxes that carry almost no architectural weight. The list
 *      lives on `data.files` (populated by App.tsx from the "contains"
 *      edges on the raw graph).
 *
 *   2. **Every node body is clickable** to drive progressive disclosure.
 *      React Flow's `onNodeClick` fires this handler; the component just
 *      needs to look focusable and hoverable. Cursor=pointer + a subtle
 *      hover ring is enough visual affordance without adding a "chevron"
 *      icon that would break the "minimal node" rule.
 *
 * Runtime overlay (Task 4): subscribes to `useRuntimeStore` for this node's
 * own id, applying `.apv-node-active`/`.apv-node-errored` (see
 * `lib/runtime-overlay.css`) to the shape wrapper. This is the ONE place
 * that logic needs to live, since every node type funnels through here —
 * no separate WorkflowNode/AgentNode/ToolNode/etc. components exist to
 * modify individually (see Task 4's `## Result` for that deviation). An
 * aggregator/cluster id never matches a real node id in the store, so the
 * lookup is always a safe no-op (falls back to "idle") for those branches.
 */

import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { NodeType } from "@shared-types/graph";
import { NODE_STYLE } from "../lib/nodeStyle";
import { useRuntimeStore } from "../state/runtime";

/** Data payload we attach to each React Flow node. */
export interface EntityNodeData {
  label: string;
  nodeType: NodeType;
  /** True if this node has revealed its children (drives the ring around expanded nodes). */
  expanded?: boolean;
  /** True if this node CAN be expanded (has outgoing edges to non-file nodes). */
  expandable?: boolean;
  /** When true, this is a UI-synthesized "aggregator" node summarizing many
   *  children of `nodeType` under a single box. Renders as e.g. "Files (12)". */
  isAggregator?: boolean;
  /** Only meaningful when isAggregator is true. */
  aggregatorCount?: number;
  /** When set, renders a small yellow ⚠ badge overlaying the node's
   *  top-right corner. The full message appears in the hover tooltip.
   *  L2 uses this to flag tools whose code couldn't be located — same
   *  message that shows in the header warnings pill, but positioned
   *  against the specific tool node so the user can see WHICH tool. */
  warning?: string;
  [key: string]: unknown;
}

export function EntityNode({ id, data }: NodeProps) {
  const d = data as EntityNodeData;
  const style = NODE_STYLE[d.nodeType];

  // Selector reads only this node's own slot -- Zustand's default equality
  // means only THIS component re-renders on a delta for `id`, not every
  // EntityNode on the graph. See this file's module docstring.
  const runtimeStatus = useRuntimeStore((s) => s.states[id]?.status ?? "idle");
  const runtimeClassName =
    runtimeStatus === "active" ? "apv-node-active" : runtimeStatus === "errored" ? "apv-node-errored" : "";

  const handles = (
    <>
      <Handle type="target" position={Position.Left} style={{ background: style.border }} />
      <Handle type="source" position={Position.Right} style={{ background: style.border }} />
    </>
  );

  // Aggregator branch — a UI-synthesized "N of type X" summary box.
  // Shape: rounded-rect with a folder-ish icon and "Files (12)" text. Uses
  // the item type's palette so it visually reads as "these are files".
  if (d.isAggregator) {
    return (
      <div
        style={{
          minWidth: 130,
          maxWidth: 180,
          padding: "6px 12px",
          background: style.fill,
          color: style.text,
          border: `${d.expanded ? 2 : 1.5}px dashed ${style.border}`,
          borderRadius: 8,
          fontSize: 13,
          fontWeight: 500,
          display: "flex",
          alignItems: "center",
          gap: 6,
          cursor: d.expandable ? "pointer" : "default",
          boxShadow: d.expanded ? `0 0 0 2px ${style.border}22` : "none",
        }}
        title={d.label}
      >
        {handles}
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
        </svg>
        <span>
          {d.label} ({d.aggregatorCount ?? 0})
        </span>
      </div>
    );
  }

  // Non-workflow nodes: single-line label, shape from NODE_STYLE. Add a subtle
  // ring when expanded so the user can see which chain they've drilled into.
  const base: React.CSSProperties = {
    width: 160,
    height: 44,
    padding: "0 12px",
    background: style.fill,
    color: style.text,
    fontSize: 13,
    fontWeight: 500,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    border: `${d.expanded ? 2 : 1.5}px solid ${style.border}`,
    textAlign: "center",
    lineHeight: 1.2,
    cursor: d.expandable ? "pointer" : "default",
    boxShadow: d.expanded ? `0 0 0 2px ${style.border}22` : "none",
  };

  // Every non-aggregator shape below gets wrapped in this container so
  // the warning badge can absolute-position against the node's bounding
  // box WITHOUT being clipped by the shape's clipPath (hexagons and
  // arrow-tags both clip). The wrapper sizes itself to the shape via
  // `display: inline-block` — React Flow's own container is what
  // positions us in the graph, so we don't need explicit dimensions here.
  const shapeWrapperStyle: React.CSSProperties = {
    position: "relative",
    display: "inline-block",
    // A little extra padding so a badge at top:-6 right:-6 still fits
    // inside our own bounding box (matters for React Flow's node-size
    // detection when it computes handle positions).
    padding: 0,
  };

  // The errored ring (runtime-overlay.css's `.apv-node-errored::after`) is a
  // pseudo-element on THIS wrapper, not on the shaped div inside it -- so it
  // can't pick up a shape's `borderRadius`/`clipPath` via plain CSS
  // inheritance (border-radius isn't an inherited property at all, and the
  // wrapper itself has neither set). Mirroring each shape's own radius/clip
  // here, in one place, keeps the ring matched to the shape it's drawn
  // around instead of always rendering as a plain rectangle.
  const RING_SHAPE: Record<typeof style.shape, { radius: string; clipPath: string }> = {
    "rounded-rect": { radius: "8px", clipPath: "none" },
    circle: { radius: "50%", clipPath: "none" },
    hexagon: { radius: "0", clipPath: "polygon(10% 0, 90% 0, 100% 50%, 90% 100%, 10% 100%, 0 50%)" },
    "arrow-tag": { radius: "0", clipPath: "polygon(0 0, 90% 0, 100% 50%, 90% 100%, 0 100%)" },
  };
  const ringShape = RING_SHAPE[style.shape];

  // Exposes this node type's own border color to runtime-overlay.css's glow
  // keyframes via `var(--apv-node-color)` -- a tool glows tool-green, an
  // agent glows agent-blue, etc. CSS custom properties aren't in React's
  // CSSProperties type, hence the cast.
  const runtimeWrapperStyle: React.CSSProperties = {
    ...shapeWrapperStyle,
    ["--apv-node-color" as string]: style.border,
    ["--apv-node-ring-radius" as string]: ringShape.radius,
    ["--apv-node-ring-clip" as string]: ringShape.clipPath,
  } as React.CSSProperties;

  // Small yellow ⚠ badge shown when data.warning is set — L2 attaches
  // messages here for tools whose code couldn't be resolved. The full
  // message is exposed via `title` so the browser's native tooltip does
  // the hover interaction — no dependency on a tooltip component.
  const warningBadge = d.warning ? (
    <span
      title={d.warning}
      aria-label={`warning: ${d.warning}`}
      style={{
        position: "absolute",
        // Inside the wrapper's own bounds so React Flow's node container
        // (which sometimes carries overflow rules we don't control) can't
        // clip us out of existence. Sits on top of the shape's top-right
        // corner — same visual language as a notification counter.
        top: 0,
        right: 0,
        width: 18,
        height: 18,
        borderRadius: "50%",
        background: "#F59E0B", // amber-500 — brighter for contrast on dark
        color: "#FFFFFF",
        border: "2px solid #78350F", // amber-950 ring
        fontSize: 12,
        lineHeight: "14px",
        fontWeight: 800,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        cursor: "help",
        // Above React Flow's edge and handle layers.
        zIndex: 10,
        boxShadow: "0 0 0 1px rgba(0,0,0,0.15), 0 1px 3px rgba(0,0,0,0.3)",
        // Prevent the badge from soaking up node-click events — it should
        // read as an indicator, and hover-tooltip works via `title`
        // without needing pointer interception.
        pointerEvents: "auto",
      }}
    >
      !
    </span>
  ) : null;

  const label = (
    <span
      style={{
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
        display: "inline-block",
        maxWidth: "100%",
      }}
      title={d.warning ? `${d.label} — ${d.warning}` : d.label}
    >
      {d.label}
    </span>
  );

  switch (style.shape) {
    case "hexagon":
      return (
        <div style={runtimeWrapperStyle} className={runtimeClassName}>
          <div style={{ ...base, clipPath: "polygon(10% 0, 90% 0, 100% 50%, 90% 100%, 10% 100%, 0 50%)" }}>
            {handles}
            {label}
          </div>
          {warningBadge}
        </div>
      );

    case "circle":
      // Function nodes: bigger circle + allow the label to wrap to 2
      // lines so identifier fragments like `internal_search_tool` show
      // as `internal_...tool` on two lines instead of `internal...`.
      // The wrapping label is fabricated inline (not the shared `label`
      // constant) because the shared one has whiteSpace:nowrap.
      return (
        <div style={runtimeWrapperStyle} className={runtimeClassName}>
          <div style={{ ...base, width: 110, height: 110, borderRadius: "50%", padding: 8 }}>
            {handles}
            <span
              style={{
                display: "-webkit-box",
                WebkitLineClamp: 3,
                WebkitBoxOrient: "vertical",
                overflow: "hidden",
                textOverflow: "ellipsis",
                lineHeight: 1.15,
                fontSize: 11,
                wordBreak: "break-word",
                textAlign: "center",
                maxWidth: "100%",
              }}
              title={d.warning ? `${d.label} — ${d.warning}` : d.label}
            >
              {d.label}
            </span>
          </div>
          {warningBadge}
        </div>
      );

    case "arrow-tag":
      return (
        <div style={runtimeWrapperStyle} className={runtimeClassName}>
          <div style={{ ...base, clipPath: "polygon(0 0, 90% 0, 100% 50%, 90% 100%, 0 100%)", paddingRight: 22 }}>
            {handles}
            {label}
          </div>
          {warningBadge}
        </div>
      );

    case "rounded-rect":
    default:
      return (
        <div style={runtimeWrapperStyle} className={runtimeClassName}>
          <div style={{ ...base, borderRadius: 8 }}>
            {handles}
            {label}
          </div>
          {warningBadge}
        </div>
      );
  }
}
