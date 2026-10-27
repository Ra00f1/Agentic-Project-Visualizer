import { describe, expect, it, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { EntityNode, type EntityNodeData } from "./EntityNode";
import { useRuntimeStore } from "../state/runtime";
import type { NodeProps } from "@xyflow/react";

// EntityNode renders <Handle> from @xyflow/react, which needs a
// ReactFlowProvider ancestor with a real store context (a raw div wrapper
// isn't enough -- @xyflow/react reads from its own internal zustand store).
function renderNode(data: EntityNodeData, id = "n1") {
  const props = {
    id,
    data: data as Record<string, unknown>,
    // Fields EntityNode doesn't read, but NodeProps requires.
    type: "entity",
    selected: false,
    dragging: false,
    isConnectable: true,
    zIndex: 0,
    positionAbsoluteX: 0,
    positionAbsoluteY: 0,
  } as unknown as NodeProps;

  return render(
    <ReactFlowProvider>
      <EntityNode {...props} />
    </ReactFlowProvider>,
  );
}

const EMPTY_DEAD_LETTER_STATS = { count: 0, dropped: 0, parseErrors: 0, nonTrace: 0, eventsResolved: 0, sources: [] };

beforeEach(() => {
  useRuntimeStore.setState({ states: {}, deadLetterStats: EMPTY_DEAD_LETTER_STATS });
});

describe("EntityNode runtime overlay", () => {
  it("applies no runtime class when idle (default, no store entry)", () => {
    const { container } = renderNode({ label: "my_tool", nodeType: "tool" });
    expect(container.querySelector(".apv-node-active")).toBeNull();
    expect(container.querySelector(".apv-node-errored")).toBeNull();
  });

  it("applies apv-node-active when the store reports this node as active", () => {
    useRuntimeStore.setState({
      states: {
        n1: { status: "active", runCount: 1, errorCount: 0, lastError: null, lastEndedAt: null, flashSeq: 0 },
      },
      deadLetterStats: EMPTY_DEAD_LETTER_STATS,
    });
    const { container } = renderNode({ label: "my_tool", nodeType: "tool" }, "n1");

    expect(container.querySelector(".apv-node-active")).not.toBeNull();
    expect(container.querySelector(".apv-node-errored")).toBeNull();
  });

  it("applies apv-node-errored when the store reports this node as errored", () => {
    useRuntimeStore.setState({
      states: {
        n1: {
          status: "errored",
          runCount: 2,
          errorCount: 1,
          lastError: { type: "RuntimeError", message: "boom", traceback: "...", at: "2026-01-01T00:00:00Z" },
          lastEndedAt: "2026-01-01T00:00:00Z",
          flashSeq: 1,
        },
      },
      deadLetterStats: EMPTY_DEAD_LETTER_STATS,
    });
    const { container } = renderNode({ label: "my_tool", nodeType: "tool" }, "n1");

    expect(container.querySelector(".apv-node-errored")).not.toBeNull();
    expect(container.querySelector(".apv-node-active")).toBeNull();
  });

  it("only reacts to state for its OWN id, not other nodes'", () => {
    useRuntimeStore.setState({
      states: {
        "some-other-node": {
          status: "active",
          runCount: 1,
          errorCount: 0,
          lastError: null,
          lastEndedAt: null,
          flashSeq: 0,
        },
      },
      deadLetterStats: EMPTY_DEAD_LETTER_STATS,
    });
    const { container } = renderNode({ label: "my_tool", nodeType: "tool" }, "n1");

    expect(container.querySelector(".apv-node-active")).toBeNull();
  });

  it("sets --apv-node-color to the node type's own border token", () => {
    const { container } = renderNode({ label: "my_agent", nodeType: "agent" }, "n1");
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.style.getPropertyValue("--apv-node-color")).toBe("var(--node-agent-border)");
  });

  it("never applies a runtime class to an aggregator node (ids never match real nodes)", () => {
    const { container } = renderNode(
      { label: "Files", nodeType: "file", isAggregator: true, aggregatorCount: 12 },
      "aggregator:file:workflow-1",
    );
    expect(container.querySelector(".apv-node-active")).toBeNull();
    expect(container.querySelector(".apv-node-errored")).toBeNull();
    expect(screen.getByText(/Files \(12\)/)).toBeInTheDocument();
  });

  it("renders the apv-node-ran flash when idle with a positive flashSeq", () => {
    useRuntimeStore.setState({
      states: {
        n1: {
          status: "idle",
          runCount: 1,
          errorCount: 0,
          lastError: null,
          lastEndedAt: "2026-01-01T00:00:00Z",
          flashSeq: 1,
        },
      },
      deadLetterStats: EMPTY_DEAD_LETTER_STATS,
    });
    const { container } = renderNode({ label: "my_tool", nodeType: "tool" }, "n1");

    expect(container.querySelector(".apv-node-ran")).not.toBeNull();
  });

  it("does NOT render the flash while active, even with a positive flashSeq", () => {
    useRuntimeStore.setState({
      states: {
        n1: {
          status: "active",
          runCount: 2,
          errorCount: 0,
          lastError: null,
          lastEndedAt: "2026-01-01T00:00:00Z",
          flashSeq: 1,
        },
      },
      deadLetterStats: EMPTY_DEAD_LETTER_STATS,
    });
    const { container } = renderNode({ label: "my_tool", nodeType: "tool" }, "n1");

    expect(container.querySelector(".apv-node-ran")).toBeNull();
  });

  it("does NOT render the flash when idle with flashSeq still at 0 (never finished a span)", () => {
    useRuntimeStore.setState({
      states: {
        n1: { status: "idle", runCount: 0, errorCount: 0, lastError: null, lastEndedAt: null, flashSeq: 0 },
      },
      deadLetterStats: EMPTY_DEAD_LETTER_STATS,
    });
    const { container } = renderNode({ label: "my_tool", nodeType: "tool" }, "n1");

    expect(container.querySelector(".apv-node-ran")).toBeNull();
  });
});
