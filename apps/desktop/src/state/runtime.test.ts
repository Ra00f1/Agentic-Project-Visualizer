import { describe, expect, it, beforeEach } from "vitest";
import { useRuntimeStore } from "./runtime";
import type {
  DeadLetterStatsMessage,
  RuntimeDeltaMessage,
  RuntimeSnapshotMessage,
  SourceStatus,
} from "@shared-types/runtime";

function resetStore() {
  useRuntimeStore.setState({
    states: {},
    connection: "connecting",
    deadLetterStats: {
      count: 0,
      dropped: 0,
      parseErrors: 0,
      nonTrace: 0,
      eventsResolved: 0,
      sources: [],
    },
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
      lastEndedAt: null,
    };
    useRuntimeStore.getState().applyDelta(delta);

    const state = useRuntimeStore.getState().states["n1"];
    expect(state).toEqual({
      status: "active",
      runCount: 3,
      errorCount: 0,
      lastError: null,
      lastEndedAt: null,
      flashSeq: 0,
    });
  });

  it("applyDelta leaves other nodes' state untouched", () => {
    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "n1",
      status: "active",
      runCount: 1,
      errorCount: 0,
      lastError: null,
      lastEndedAt: null,
    });
    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "n2",
      status: "errored",
      runCount: 1,
      errorCount: 1,
      lastError: { type: "RuntimeError", message: "boom", traceback: "...", at: "2026-01-01T00:00:00Z" },
      lastEndedAt: "2026-01-01T00:00:00Z",
    });

    const states = useRuntimeStore.getState().states;
    expect(states["n1"]?.status).toBe("active");
    expect(states["n2"]?.status).toBe("errored");
    expect(states["n2"]?.lastError?.message).toBe("boom");
  });

  it("applyDelta increments flashSeq when a completion was never shown live (Task 24, D2)", () => {
    // Simulates OTLP: every delta for this node arrives already completed --
    // `previous?.status` is never "active" because this client never saw a
    // live "active" delta for it, only ever idle/errored completions.
    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "n1",
      status: "idle",
      runCount: 1,
      errorCount: 0,
      lastError: null,
      lastEndedAt: "2026-01-01T00:00:00Z",
    });
    expect(useRuntimeStore.getState().states["n1"]?.flashSeq).toBe(1);

    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "n1",
      status: "idle",
      runCount: 2,
      errorCount: 0,
      lastError: null,
      lastEndedAt: "2026-01-01T00:00:05Z",
    });
    expect(useRuntimeStore.getState().states["n1"]?.flashSeq).toBe(2);
  });

  it("applyDelta does NOT flash a span that was already shown active (Task 24, D2)", () => {
    // A real live span: an "active" delta arrives first (the glow already
    // carries the "running" signal), then the completion -- must NOT also flash.
    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "n1",
      status: "active",
      runCount: 1,
      errorCount: 0,
      lastError: null,
      lastEndedAt: null,
    });
    expect(useRuntimeStore.getState().states["n1"]?.flashSeq).toBe(0);

    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "n1",
      status: "idle",
      runCount: 1,
      errorCount: 0,
      lastError: null,
      lastEndedAt: "2026-01-01T00:00:00Z",
    });
    expect(useRuntimeStore.getState().states["n1"]?.flashSeq).toBe(0);
  });

  it("applyDelta does NOT increment flashSeq when lastEndedAt is unchanged", () => {
    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "n1",
      status: "idle",
      runCount: 1,
      errorCount: 0,
      lastError: null,
      lastEndedAt: "2026-01-01T00:00:00Z",
    });
    expect(useRuntimeStore.getState().states["n1"]?.flashSeq).toBe(1);

    // Another delta for the same node, but lastEndedAt is the SAME value
    // as before -- must not double-flash on a repeat/unrelated delta.
    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "n1",
      status: "idle",
      runCount: 2,
      errorCount: 0,
      lastError: null,
      lastEndedAt: "2026-01-01T00:00:00Z",
    });
    expect(useRuntimeStore.getState().states["n1"]?.flashSeq).toBe(1);
  });

  it("applySnapshot replaces state wholesale (not merged) and never bumps flashSeq", () => {
    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "stale",
      status: "active",
      runCount: 1,
      errorCount: 0,
      lastError: null,
      lastEndedAt: null,
    });

    const snapshot: RuntimeSnapshotMessage = {
      type: "snapshot",
      states: [
        {
          nodeId: "n1",
          status: "idle",
          runCount: 5,
          errorCount: 0,
          lastError: null,
          lastEndedAt: "2026-01-01T00:00:00Z",
        },
        { nodeId: "n2", status: "errored", runCount: 2, errorCount: 1, lastError: null, lastEndedAt: null },
      ],
    };
    useRuntimeStore.getState().applySnapshot(snapshot);

    const states = useRuntimeStore.getState().states;
    expect(Object.keys(states).sort()).toEqual(["n1", "n2"]);
    expect(states["stale"]).toBeUndefined(); // wholesale replace, not merge
    expect(states["n1"]?.runCount).toBe(5);
    // A snapshot can carry a non-null lastEndedAt (a node that already
    // finished a span before this client connected) without ever flashing.
    expect(states["n1"]?.flashSeq).toBe(0);
    expect(states["n2"]?.flashSeq).toBe(0);
  });

  it("applyReset clears all tracked state", () => {
    useRuntimeStore.getState().applyDelta({
      type: "delta",
      nodeId: "n1",
      status: "active",
      runCount: 1,
      errorCount: 0,
      lastError: null,
      lastEndedAt: null,
    });
    expect(Object.keys(useRuntimeStore.getState().states)).toHaveLength(1);

    useRuntimeStore.getState().applyReset();

    expect(useRuntimeStore.getState().states).toEqual({});
  });

  it("applyDeadLetterStats maps the wire field names to the store's shape, including the Task 22 sources list", () => {
    const sources: SourceStatus[] = [
      {
        methodId: "apv_live_file",
        label: "Live trace (apv_trace)",
        state: "running",
        detail: "/tmp/trace.jsonl",
        eventsReceived: 12,
        eventsIgnored: 3,
      },
    ];
    const message: DeadLetterStatsMessage = {
      type: "dead_letter_stats",
      count: 3,
      droppedCount: 7,
      parseErrorCount: 2,
      nonTraceCount: 5,
      eventsResolved: 9,
      sources,
    };
    useRuntimeStore.getState().applyDeadLetterStats(message);

    expect(useRuntimeStore.getState().deadLetterStats).toEqual({
      count: 3,
      dropped: 7,
      parseErrors: 2,
      nonTrace: 5,
      eventsResolved: 9,
      sources,
    });
  });

  it("setConnection updates the connection field", () => {
    expect(useRuntimeStore.getState().connection).toBe("connecting");

    useRuntimeStore.getState().setConnection("open");
    expect(useRuntimeStore.getState().connection).toBe("open");

    useRuntimeStore.getState().setConnection("closed");
    expect(useRuntimeStore.getState().connection).toBe("closed");
  });
});
