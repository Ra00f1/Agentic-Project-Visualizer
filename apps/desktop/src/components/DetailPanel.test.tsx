import { describe, expect, it, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { DetailPanel } from "./DetailPanel";
import { useRuntimeStore } from "../state/runtime";
import type { GraphNode } from "@shared-types/graph";

const NODE: GraphNode = {
  id: "mongo:db.tools:1",
  type: "tool",
  name: "lookup_kb",
  provenance: { source: "l1_entity", sourceRef: "db.tools:1", scannedAt: "2026-01-01T00:00:00Z" },
  attributes: {},
};

beforeEach(() => {
  useRuntimeStore.setState({ states: {}, deadLetterStats: { count: 0, dropped: 0, parseErrors: 0 } });
});

describe("DetailPanel runtime section", () => {
  it("does not show a Runtime section when the node has no tracked state", () => {
    render(<DetailPanel node={NODE} onClose={vi.fn()} />);
    expect(screen.queryByText("Runtime")).toBeNull();
  });

  it("does not show a Runtime section when the node is idle", () => {
    useRuntimeStore.setState({
      states: { [NODE.id]: { status: "idle", runCount: 2, errorCount: 0, lastError: null } },
      deadLetterStats: { count: 0, dropped: 0, parseErrors: 0 },
    });
    render(<DetailPanel node={NODE} onClose={vi.fn()} />);
    expect(screen.queryByText("Runtime")).toBeNull();
  });

  it("shows the Runtime section with status/run count/error count when active", () => {
    useRuntimeStore.setState({
      states: { [NODE.id]: { status: "active", runCount: 4, errorCount: 0, lastError: null } },
      deadLetterStats: { count: 0, dropped: 0, parseErrors: 0 },
    });
    render(<DetailPanel node={NODE} onClose={vi.fn()} />);

    expect(screen.getByText("Runtime")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
  });

  it("shows last-error type/message/timestamp and a copy-stack button when errored", () => {
    useRuntimeStore.setState({
      states: {
        [NODE.id]: {
          status: "errored",
          runCount: 3,
          errorCount: 1,
          lastError: {
            type: "RuntimeError",
            message: "simulated triage failure",
            traceback: "Traceback...\nRuntimeError: simulated triage failure",
            at: "2026-01-01T00:00:00Z",
          },
        },
      },
      deadLetterStats: { count: 0, dropped: 0, parseErrors: 0 },
    });
    render(<DetailPanel node={NODE} onClose={vi.fn()} />);

    expect(screen.getByText("errored")).toBeInTheDocument();
    expect(screen.getByText("RuntimeError")).toBeInTheDocument();
    expect(screen.getByText("simulated triage failure")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy stack" })).toBeInTheDocument();
  });
});
