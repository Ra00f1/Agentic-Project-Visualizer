import { describe, expect, it } from "vitest";
import { layoutGraph, _snapSingleParentChildren } from "./layout";
import type { Node as RfNode, Edge as RfEdge } from "@xyflow/react";

/**
 * Task 7: single-parent children (tool -> function, and generalized to any
 * node with exactly one real incoming edge) should align with their parent's
 * y-coordinate rather than drifting to dagre's ancestry-average position.
 *
 * Two layers of coverage, per this task's own Design section:
 *  - Integration tests drive the public `layoutGraph` entry point (both
 *    rankers: "longest-path" is the Tiered-mode BFS engine, "network-simplex"
 *    is Compact/Organic's real dagre pass -- see App.tsx's `layoutRanker`).
 *  - Direct unit tests of the exported `_snapSingleParentChildren` cover the
 *    overlap-skip and multi-parent-no-op edge cases against known input
 *    positions, which would be fragile to force through dagre's opaque
 *    internals (see that function's own doc comment for why it's exported).
 */

function node(id: string, nodeType: string, position = { x: 0, y: 0 }): RfNode {
  return { id, type: "entity", position, data: { nodeType, label: id } };
}

function edge(source: string, target: string): RfEdge {
  return { id: `${source}->${target}`, source, target };
}

function byId(nodes: RfNode[]): Map<string, RfNode> {
  return new Map(nodes.map((n) => [n.id, n]));
}

describe("layoutGraph — single-parent y-snap (integration)", () => {
  // workflow -> agent -> [tool1, tool2] (a fan-out, so dagre/BFS has a real
  // choice to make about the agent's y) -> tool1 -> function (single parent).
  const NODES: RfNode[] = [
    node("wf", "workflow"),
    node("agent", "agent"),
    node("tool1", "tool"),
    node("tool2", "tool"),
    node("fn", "function"),
  ];
  const EDGES: RfEdge[] = [
    edge("wf", "agent"),
    edge("agent", "tool1"),
    edge("agent", "tool2"),
    edge("tool1", "fn"),
  ];

  it.each([
    ["tiered (longest-path / BFS)", "longest-path" as const],
    ["compact (network-simplex / dagre)", "network-simplex" as const],
  ])("snaps the function node to its sole parent tool's y — %s", (_label, ranker) => {
    const { nodes } = layoutGraph(NODES, EDGES, { ranker });
    const positions = byId(nodes);
    const tool1 = positions.get("tool1")!;
    const fn = positions.get("fn")!;
    expect(fn.position.y).toBe(tool1.position.y);
  });

  it("does not crash and still positions a shared (multi-parent) child when routed through the full layoutGraph pipeline", () => {
    // summarize_url-style case: two agents both use the same tool. Whether
    // dagre's own placement happens to coincide with one parent's y in any
    // given fixture isn't something this test can pin down (that's an
    // implementation detail of dagre's network-simplex solver, not this
    // task's concern) -- the precise "multi-parent must not snap" invariant
    // is covered deterministically below, against `_snapSingleParentChildren`
    // directly with known input positions. This is a smoke check that the
    // multi-parent case flows through the real pipeline without error.
    const nodes: RfNode[] = [
      node("wf", "workflow"),
      node("agentA", "agent"),
      node("agentB", "agent"),
      node("sharedTool", "tool"),
    ];
    const edges: RfEdge[] = [
      edge("wf", "agentA"),
      edge("wf", "agentB"),
      edge("agentA", "sharedTool"),
      edge("agentB", "sharedTool"),
    ];
    const { nodes: laidOut } = layoutGraph(nodes, edges, { ranker: "network-simplex" });
    expect(byId(laidOut).get("sharedTool")!.position).toBeDefined();
  });
});

describe("_snapSingleParentChildren (direct unit tests)", () => {
  it("snaps a single-parent node onto its parent's y when nothing occupies that slot", () => {
    const positioned: RfNode[] = [
      node("parent", "tool", { x: 0, y: 100 }),
      node("child", "function", { x: 300, y: 250 }),
    ];
    const edges: RfEdge[] = [edge("parent", "child")];
    _snapSingleParentChildren(positioned, edges);
    expect(byId(positioned).get("child")!.position.y).toBe(100);
  });

  it("does NOT snap when a node already occupies the target (rank, y) slot", () => {
    const positioned: RfNode[] = [
      node("parent", "tool", { x: 0, y: 100 }),
      // Already sitting exactly where "child" would want to snap to --
      // same rank (x) as child, same y as the snap target.
      node("occupant", "function", { x: 300, y: 100 }),
      node("child", "function", { x: 300, y: 250 }),
    ];
    const edges: RfEdge[] = [edge("parent", "child")];
    _snapSingleParentChildren(positioned, edges);
    expect(byId(positioned).get("child")!.position.y).toBe(250); // unchanged
  });

  it("does not move a node with two real parents (no single-parent classification)", () => {
    const positioned: RfNode[] = [
      node("parentA", "agent", { x: 0, y: 0 }),
      node("parentB", "agent", { x: 0, y: 400 }),
      node("shared", "tool", { x: 300, y: 180 }),
    ];
    const edges: RfEdge[] = [edge("parentA", "shared"), edge("parentB", "shared")];
    _snapSingleParentChildren(positioned, edges);
    expect(byId(positioned).get("shared")!.position.y).toBe(180); // unchanged
  });

  it("ignores synthetic (aggregator/cluster/clone) edges when counting parents", () => {
    // "real" tool -> function edge (single parent) PLUS a synthetic
    // aggregator "parent" edge that must not count toward the parent total
    // -- otherwise this would be misclassified as multi-parent and never
    // snap, exactly the Risk this task's own Design section calls out.
    const positioned: RfNode[] = [
      node("tool1", "tool", { x: 0, y: 100 }),
      node("fn", "function", { x: 300, y: 250 }),
    ];
    const edges: RfEdge[] = [
      edge("tool1", "fn"),
      edge("aggregator:file:tool1", "fn"),
    ];
    _snapSingleParentChildren(positioned, edges);
    expect(byId(positioned).get("fn")!.position.y).toBe(100);
  });

  it("ignores a clone edge (<originalId>@<cloneScope> shape) when counting parents", () => {
    // Mirrors computeView.ts's actual clone-id construction:
    // `${e.targetId}@${cloneScope}`, where cloneScope is itself a real node
    // id (always containing a ":"). Distinct from the email-like-id test
    // below, which must NOT be treated as synthetic.
    const positioned: RfNode[] = [
      node("tool1", "tool", { x: 0, y: 100 }),
      node("fn", "function", { x: 300, y: 250 }),
    ];
    const edges: RfEdge[] = [
      edge("tool1", "fn"),
      edge("mongo:db.tools:2@mongo:db.agents:9", "fn"),
    ];
    _snapSingleParentChildren(positioned, edges);
    expect(byId(positioned).get("fn")!.position.y).toBe(100);
  });

  it("does NOT treat a real node id containing '@' (e.g. an email-mapped id_field) as synthetic", () => {
    // Regression test: a user can map a collection's id_field to an
    // email-like value, producing a real node id such as
    // "mongo:db.users:owner@example.com" -- unlike a clone id, there's no
    // ":" after the "@", so this must still count as a REAL parent edge.
    // Two real parents -> multi-parent no-op (dagre's placement stands),
    // proving the edge was counted, not silently excluded.
    const positioned: RfNode[] = [
      node("mongo:db.users:owner@example.com", "user", { x: 0, y: 0 }),
      node("otherParent", "agent", { x: 0, y: 400 }),
      node("shared", "tool", { x: 300, y: 180 }),
    ];
    const edges: RfEdge[] = [
      edge("mongo:db.users:owner@example.com", "shared"),
      edge("otherParent", "shared"),
    ];
    _snapSingleParentChildren(positioned, edges);
    expect(byId(positioned).get("shared")!.position.y).toBe(180); // unchanged -- correctly classified as 2 real parents
  });

  it("is a no-op for a node with no parent edges", () => {
    const positioned: RfNode[] = [node("lonely", "workflow", { x: 0, y: 42 })];
    _snapSingleParentChildren(positioned, []);
    expect(byId(positioned).get("lonely")!.position.y).toBe(42);
  });

  it("converges (doesn't hang or oscillate) on a mutual-recursion call cycle", () => {
    // L3's `calls` edges are a real function-call graph -- mutual recursion
    // (funcA calls funcB calls funcA) is a genuine shape that graph can
    // produce, and it makes each of the two nodes the OTHER's sole parent.
    // This isn't hypothetical: it's exactly the kind of cycle the fixpoint
    // loop's `SNAP_MAX_ITERATIONS` cap exists to bound (see that constant's
    // doc comment).
    //
    // Positions here use DIFFERENT x (ranks), matching what this function
    // actually ever receives in production: it's only ever called on
    // already-dagre/BFS-laid-out positions, and dagre breaks cycles before
    // rank assignment, so two nodes joined by an edge -- even one side of a
    // mutual pair -- are never placed at the same x. (An earlier version of
    // this test used the SAME x for both and failed: the overlap check
    // correctly refused to move funcA on top of funcB, since funcB's
    // position literally IS funcA's snap target when the parent's parent is
    // the child. That's the algorithm working correctly against an
    // unrealistic input, not a bug -- fixed here to assert what the
    // fixpoint loop actually needs to guarantee: convergence to a shared y
    // for a realistic cyclic input, not an infinite bounce.)
    const positioned: RfNode[] = [
      node("funcA", "function", { x: 0, y: 100 }),
      node("funcB", "function", { x: 300, y: 300 }),
    ];
    const edges: RfEdge[] = [edge("funcA", "funcB"), edge("funcB", "funcA")];
    _snapSingleParentChildren(positioned, edges);
    const byNode = byId(positioned);
    expect(byNode.get("funcA")!.position.y).toBe(byNode.get("funcB")!.position.y);
  });
});
