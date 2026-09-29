import { describe, expect, it } from "vitest";
import { computeView } from "./computeView";
import type { Graph, GraphEdge, GraphNode } from "@shared-types/graph";

/**
 * Regression coverage for task 5: "clicking a tool node reveals its
 * L2-resolved function node" had no test exercising the tool -> function
 * link specifically (test_l2_scanner.py covers the backend producing the
 * node/edge; nothing on the frontend asserted computeView actually surfaces
 * it once expanded). Live investigation against the real mock_agent graph
 * found the mechanism already works end-to-end — see task 5's Result for
 * the full diagnosis — but that doesn't mean it can't regress silently
 * later, which is exactly what this file guards against.
 */

const PROVENANCE = { source: "test", sourceRef: "test", scannedAt: "2026-01-01T00:00:00Z" };

function node(id: string, type: GraphNode["type"], name = id): GraphNode {
  return { id, type, name, provenance: PROVENANCE, attributes: {} };
}

function edge(sourceId: string, targetId: string, kind: GraphEdge["kind"]): GraphEdge {
  return { sourceId, targetId, kind, provenance: PROVENANCE, attributes: {} };
}

/** workflow -> agent (uses) -> tool (uses) -> function (implements) -> callee (calls). */
const GRAPH: Graph = {
  nodes: [
    node("wf-1", "workflow", "Workflow"),
    node("agent-1", "agent", "Agent"),
    node("tool-1", "tool", "Tool"),
    node("fn-1", "function", "some_tool_fn"),
    node("fn-2", "function", "callee_fn"),
  ],
  edges: [
    edge("wf-1", "agent-1", "uses"),
    edge("agent-1", "tool-1", "uses"),
    edge("tool-1", "fn-1", "implements"),
    edge("fn-1", "fn-2", "calls"),
  ],
  errors: [],
};

function labelsOf(nodes: ReturnType<typeof computeView>["nodes"]): string[] {
  return nodes.map((n) => n.id);
}

describe("computeView — tool -> function expansion", () => {
  it("shows only the workflow when nothing is expanded", () => {
    const { nodes } = computeView(GRAPH, new Set(), false);
    expect(labelsOf(nodes)).toEqual(["wf-1"]);
  });

  it("reveals the agent once the workflow is expanded", () => {
    const { nodes } = computeView(GRAPH, new Set(["wf-1"]), false);
    expect(labelsOf(nodes)).toEqual(["wf-1", "agent-1"]);
  });

  it("reveals the tool once the workflow and agent are expanded", () => {
    const { nodes } = computeView(GRAPH, new Set(["wf-1", "agent-1"]), false);
    expect(labelsOf(nodes)).toEqual(["wf-1", "agent-1", "tool-1"]);
  });

  it("reveals the function node once the workflow, agent, and tool are all expanded", () => {
    const { nodes, edges } = computeView(GRAPH, new Set(["wf-1", "agent-1", "tool-1"]), false);
    expect(labelsOf(nodes)).toEqual(["wf-1", "agent-1", "tool-1", "fn-1"]);
    expect(edges.some((e) => e.source === "tool-1" && e.target === "fn-1")).toBe(true);
  });

  it("marks the tool as expandable only once its implements edge is reachable", () => {
    const { nodes } = computeView(GRAPH, new Set(["wf-1", "agent-1"]), false);
    const tool = nodes.find((n) => n.id === "tool-1");
    expect((tool?.data as { expandable?: boolean } | undefined)?.expandable).toBe(true);
  });

  it("reveals an L3 callee once the function node is also expanded (nested expansion)", () => {
    const { nodes, edges } = computeView(
      GRAPH,
      new Set(["wf-1", "agent-1", "tool-1", "fn-1"]),
      false,
    );
    expect(labelsOf(nodes)).toEqual(["wf-1", "agent-1", "tool-1", "fn-1", "fn-2"]);
    expect(edges.some((e) => e.source === "fn-1" && e.target === "fn-2")).toBe(true);
  });

  it("does not reveal the function node if the tool itself was never expanded", () => {
    // Workflow and agent expanded, but NOT the tool -- even though the tool
    // is visible (revealed by the agent), its own outgoing edges aren't
    // walked until it's in expandedIds too. This is the exact distinction
    // task 5 set out to diagnose: "visible" and "expanded" are different.
    const { nodes } = computeView(GRAPH, new Set(["wf-1", "agent-1"]), false);
    expect(nodes.some((n) => n.id === "fn-1")).toBe(false);
  });
});
