/**
 * Color + shape encoding per node type.
 *
 * CLAUDE.md §7 says: "A node shows exactly one label: its `name`. Type is
 * encoded by color and by shape." This module is that encoding, in one place.
 * When we add a new node type (function, endpoint), we extend the map here
 * and nothing else in the UI has to change.
 *
 * All colors are CSS variables defined in `index.css`, so a theme switch
 * (`.dark` on `<html>`) flips the whole palette without any component
 * needing to know about the theme. If you're adding a new type, add its
 * tokens to `:root` and `.dark` in `index.css` first, then wire them up here.
 */

import type { NodeType } from "@shared-types/graph";

export type NodeShape = "rounded-rect" | "hexagon" | "circle" | "arrow-tag";

export interface NodeStyle {
  /** CSS background color for the node body. */
  fill: string;
  /** CSS border color. */
  border: string;
  /** Which shape to render — the custom node component branches on this. */
  shape: NodeShape;
  /** Text color chosen for legibility on the fill. */
  text: string;
}

/**
 * Central mapping. Every NodeType MUST have an entry — TypeScript enforces
 * this at compile time via the `Record<NodeType, ...>` shape.
 */
export const NODE_STYLE: Record<NodeType, NodeStyle> = {
  workflow: { fill: "var(--node-workflow-bg)", border: "var(--node-workflow-border)", shape: "rounded-rect", text: "var(--node-workflow-text)" },
  agent:    { fill: "var(--node-agent-bg)",    border: "var(--node-agent-border)",    shape: "rounded-rect", text: "var(--node-agent-text)" },
  model:    { fill: "var(--node-model-bg)",    border: "var(--node-model-border)",    shape: "rounded-rect", text: "var(--node-model-text)" },
  tool:     { fill: "var(--node-tool-bg)",     border: "var(--node-tool-border)",     shape: "hexagon",      text: "var(--node-tool-text)" },
  prompt:   { fill: "var(--node-prompt-bg)",   border: "var(--node-prompt-border)",   shape: "rounded-rect", text: "var(--node-prompt-text)" },
  user:     { fill: "var(--node-user-bg)",     border: "var(--node-user-border)",     shape: "circle",       text: "var(--node-user-text)" },
  file:     { fill: "var(--node-file-bg)",     border: "var(--node-file-border)",     shape: "rounded-rect", text: "var(--node-file-text)" },
  function: { fill: "var(--node-function-bg)", border: "var(--node-function-border)", shape: "circle",       text: "var(--node-function-text)" },
  endpoint: { fill: "var(--node-endpoint-bg)", border: "var(--node-endpoint-border)", shape: "arrow-tag",    text: "var(--node-endpoint-text)" },
};

/** Edge color per relationship kind. Uses CSS variables so themes can flip
 *  them independently later — for now both themes use the same values. */
export const EDGE_COLOR: Record<string, string> = {
  uses: "var(--edge-uses)",
  owns: "var(--edge-owns)",
  contains: "var(--edge-contains)",
  delegates_to: "var(--edge-delegates_to)",
  implements: "var(--edge-implements)",
  calls: "var(--edge-calls)",
  exposes: "var(--edge-exposes)",
};
