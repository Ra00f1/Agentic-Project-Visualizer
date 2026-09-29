import { describe, expect, it, beforeEach } from "vitest";
import { useRuntimeStore } from "./runtime";
import type {
  DeadLetterStatsMessage,
  RuntimeDeltaMessage,
  RuntimeSnapshotMessage,
} from "@shared-types/runtime";

function resetStore() {
  useRuntimeStore.setState({
    states: {},
    deadLetterStats: { count: 0, dropped: 0, parseErrors: 0 },
  });
}

beforeEach(() => {
  resetStore();
});

describe("useRuntimeStore", () => {
  it("applyDelta adds/updates a single node's state", () => {
    const delta: RuntimeDeltaMessage = {
      type: "delta",
      nodeId: "n1",
      status: "active",
      runCount: 3,
      errorCount: 0,
      lastError: null,
    };
    useRuntimeStore.getState().applyDelta(delta);

    const state = useRuntimeStore.getState().states["n1"];
    expect(state).toEqual({ status: "active", runCount: 3, errorCount: 0, lastError: null });
  });

  it("applyDelta leaves other nodes' state untouched", () => {
    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "n1",
      status: "active",
      runCount: 1,
      errorCount: 0,
      lastError: null,
    });
    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "n2",
      status: "errored",
      runCount: 1,
      errorCount: 1,
      lastError: { type: "RuntimeError", message: "boom", traceback: "...", at: "2026-01-01T00:00:00Z" },
    });

    const states = useRuntimeStore.getState().states;
    expect(states["n1"]?.status).toBe("active");
    expect(states["n2"]?.status).toBe("errored");
    expect(states["n2"]?.lastError?.message).toBe("boom");
  });

  it("applySnapshot replaces state wholesale (not merged)", () => {
    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "stale",
      status: "active",
      runCount: 1,
      errorCount: 0,
      lastError: null,
    });

    const snapshot: RuntimeSnapshotMessage = {
      type: "snapshot",
      states: [
        { nodeId: "n1", status: "idle", runCount: 5, errorCount: 0, lastError: null },
        { nodeId: "n2", status: "errored", runCount: 2, errorCount: 1, lastError: null },
      ],
    };
    useRuntimeStore.getState().applySnapshot(snapshot);

    const states = useRuntimeStore.getState().states;
    expect(Object.keys(states).sort()).toEqual(["n1", "n2"]);
    expect(states["stale"]).toBeUndefined(); // wholesale replace, not merge
    expect(states["n1"]?.runCount).toBe(5);
  });

  it("applyReset clears all tracked state", () => {
    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "n1",
      status: "active",
      runCount: 1,
      errorCount: 0,
      lastError: null,
    });
    expect(Object.keys(useRuntimeStore.getState().states)).toHaveLength(1);

    useRuntimeStore.getState().applyReset();

    expect(useRuntimeStore.getState().states).toEqual({});
  });

  it("applyDeadLetterStats maps the wire field names to the store's shape", () => {
    const message: DeadLetterStatsMessage = {
      type: "dead_letter_stats",
      count: 3,
      droppedCount: 7,
      parseErrorCount: 2,
    };
    useRuntimeStore.getState().applyDeadLetterStats(message);

    expect(useRuntimeStore.getState().deadLetterStats).toEqual({
      count: 3,
      dropped: 7,
      parseErrors: 2,
    });
  });
});
