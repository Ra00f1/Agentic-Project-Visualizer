/**
 * LegendBar — a thin strip below the main header that translates the
 * graph's visual vocabulary (node fill + border color, edge stroke + dash)
 * into words.
 *
 * Why bother: a first-time user opens the app and sees a bunch of pastel
 * shapes connected by mixed-color lines. There is no in-canvas legend on
 * the graph itself (per CLAUDE.md §7 the nodes stay minimal), so the
 * mapping has to live somewhere reachable at a glance. That "somewhere"
 * is here.
 *
 * Two design choices worth calling out:
 *
 *   1. **Live counts drive visibility.** We only show a node-type chip
 *      when at least one node of that type is currently on-screen, and
 *      the same for edge kinds. Otherwise the legend fills up with node
 *      types the user's project never uses ("endpoint" is common until
 *      L4 lands). The count comes from what React Flow is rendering
 *      right now, so it stays in sync with expand/collapse and filters.
 *
 *   2. **Every swatch mirrors its node/edge exactly.** The node chip
 *      uses the same fill + border tokens as the shape in the graph;
 *      the edge sample uses the same stroke color, width, and dash
 *      pattern. Changing a token in `index.css` (or a stroke style in
 *      App.tsx's edge factory) updates the legend for free — no
 *      duplicated hex values here.
 */

import type { Node as RfNode, Edge as RfEdge } from "@xyflow/react";
import type { NodeType } from "@shared-types/graph";
import { EDGE_COLOR, NODE_STYLE } from "../lib/nodeStyle";
import type { EntityNodeData } from "./EntityNode";

/** Display order for node types. Follows the pipeline direction the graph
 *  lays out along (workflow on the left, endpoint on the right) so the
 *  legend reads like a mini table of contents for the graph. */
const NODE_TYPE_ORDER: NodeType[] = [
  "workflow",
  "agent",
  "model",
  "tool",
  "prompt",
  "user",
  "file",
  "function",
  "endpoint",
];

/** Display order for edge kinds. Groups the L1 relationship kinds (uses,
 *  owns, contains, delegates_to) before the L2-and-later ones
 *  (implements, calls, exposes) so someone scanning the bar sees the
 *  most-frequent kinds first. */
const EDGE_KIND_ORDER: string[] = [
  "uses",
  "owns",
  "contains",
  "delegates_to",
  "implements",
  "calls",
  "exposes",
];

/** Human-readable labels. Keeps the source-of-truth for how a kind is
 *  named in the UI in one place — if we ever rename "delegates_to" to
 *  "delegates", the legend picks it up here and the edge factory picks
 *  it up from wherever it does. */
const NODE_TYPE_LABEL: Record<NodeType, string> = {
  workflow: "Workflow",
  agent: "Agent",
  model: "Model",
  tool: "Tool",
  prompt: "Prompt",
  user: "User",
  file: "File",
  function: "Function",
  endpoint: "Endpoint",
};

const EDGE_KIND_LABEL: Record<string, string> = {
  uses: "uses",
  owns: "owns",
  contains: "contains",
  delegates_to: "delegates",
  implements: "implements",
  calls: "calls",
  exposes: "exposes",
};

/** delegates_to and calls render as dashed lines in the graph — mirror
 *  that here so the legend swatch looks like the edge it names. Kept in
 *  a small map instead of scattered ternaries so adding another dashed
 *  kind later is one line. */
const DASHED_EDGES: ReadonlySet<string> = new Set(["delegates_to", "calls"]);

export interface LegendBarProps {
  nodes: RfNode[];
  edges: RfEdge[];
}

export function LegendBar({ nodes, edges }: LegendBarProps) {
  // Live counts. One pass each — cheap enough to run on every render, no
  // memoization needed at this scale (a few hundred nodes tops).
  const nodeCounts: Partial<Record<NodeType, number>> = {};
  for (const n of nodes) {
    const t = (n.data as EntityNodeData | undefined)?.nodeType;
    if (!t) continue;
    // Aggregator nodes (files-summary boxes) shouldn't inflate the
    // real-type count — they're a UI convenience, not entities.
    if ((n.data as EntityNodeData).isAggregator) continue;
    nodeCounts[t] = (nodeCounts[t] ?? 0) + 1;
  }

  const edgeCounts: Record<string, number> = {};
  for (const e of edges) {
    const k = (e.data as { kind?: string } | undefined)?.kind;
    if (!k) continue;
    edgeCounts[k] = (edgeCounts[k] ?? 0) + 1;
  }

  // Filter to what's actually visible. Empty type/kind → skip.
  const activeTypes = NODE_TYPE_ORDER.filter((t) => (nodeCounts[t] ?? 0) > 0);
  const activeKinds = EDGE_KIND_ORDER.filter((k) => (edgeCounts[k] ?? 0) > 0);

  // If nothing is on screen (before graph loads / after aggressive
  // filtering), render nothing rather than an empty strip. Keeps the
  // toolbar from looking broken.
  if (activeTypes.length === 0 && activeKinds.length === 0) return null;

  return (
    <div
      role="region"
      aria-label="Graph legend"
      className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-neutral-200 bg-white px-4 py-1.5 dark:border-neutral-800 dark:bg-neutral-950"
    >
      {activeTypes.length > 0 && (
        <>
          <span className="text-[10px] font-semibold uppercase tracking-wider text-neutral-500 dark:text-neutral-400">
            Types
          </span>
          {activeTypes.map((t) => (
            <NodeTypeChip key={t} type={t} count={nodeCounts[t]!} />
          ))}
        </>
      )}
      {activeTypes.length > 0 && activeKinds.length > 0 && (
        <span
          aria-hidden
          className="mx-1 h-4 w-px bg-neutral-200 dark:bg-neutral-800"
        />
      )}
      {activeKinds.length > 0 && (
        <>
          <span className="text-[10px] font-semibold uppercase tracking-wider text-neutral-500 dark:text-neutral-400">
            Edges
          </span>
          {activeKinds.map((k) => (
            <EdgeKindChip key={k} kind={k} count={edgeCounts[k]!} />
          ))}
        </>
      )}
    </div>
  );
}

/**
 * One node-type chip: a colored pill matching that type's fill+border
 * tokens, with the type label and the current live count.
 *
 * Uses the NODE_STYLE map from nodeStyle.ts so the color references stay
 * CSS variables — theme flips (light/dark) update the chip without any
 * work here.
 */
function NodeTypeChip({ type, count }: { type: NodeType; count: number }) {
  const style = NODE_STYLE[type];
  return (
    <span
      title={`${count} ${NODE_TYPE_LABEL[type]} node${count === 1 ? "" : "s"} visible`}
      className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium"
      style={{
        background: style.fill,
        color: style.text,
        borderColor: style.border,
      }}
    >
      <span
        aria-hidden
        className="inline-block h-2 w-2 rounded-sm border"
        style={{ background: style.fill, borderColor: style.border }}
      />
      {NODE_TYPE_LABEL[type]}
      <span className="opacity-70">· {count}</span>
    </span>
  );
}

/**
 * One edge-kind chip: a short SVG stroke sample (solid or dashed to
 * match the actual edge rendering) plus the kind's label and count.
 *
 * SVG width 24 is just enough to show the dash pattern; keeping the
 * dasharray identical to the value the edge factory sets in App.tsx
 * ("6 4" for delegates_to, "3 2" for calls in the mockup) is what makes
 * the legend feel like the same visual language.
 */
function EdgeKindChip({ kind, count }: { kind: string; count: number }) {
  const color = EDGE_COLOR[kind] ?? "#4B5563";
  const dashed = DASHED_EDGES.has(kind);
  return (
    <span
      title={`${count} ${EDGE_KIND_LABEL[kind] ?? kind} edge${count === 1 ? "" : "s"} visible`}
      className="inline-flex items-center gap-1.5 text-[11px] text-neutral-700 dark:text-neutral-300"
    >
      <svg width="24" height="8" aria-hidden>
        <line
          x1="0"
          y1="4"
          x2="24"
          y2="4"
          stroke={color}
          strokeWidth={dashed ? 2 : 1.75}
          strokeDasharray={
            kind === "delegates_to" ? "6 3" : kind === "calls" ? "3 2" : undefined
          }
          strokeLinecap="round"
        />
      </svg>
      <span>{EDGE_KIND_LABEL[kind] ?? kind}</span>
      <span className="text-neutral-400 dark:text-neutral-500">· {count}</span>
    </span>
  );
}
