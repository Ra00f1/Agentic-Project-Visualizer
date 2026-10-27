import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { RuntimeStatusChip } from "./RuntimeStatusChip";
import { useRuntimeStore, type DeadLetterStats, type RuntimeConnectionState } from "../state/runtime";
import type { SourceStatus, TraceMethodInfo } from "@shared-types/runtime";

vi.mock("../api", () => ({
  fetchTraceMethods: vi.fn(),
}));

const BASE_STATS: DeadLetterStats = {
  count: 0,
  dropped: 0,
  parseErrors: 0,
  nonTrace: 0,
  eventsResolved: 0,
  sources: [],
};

function source(overrides: Partial<SourceStatus> = {}): SourceStatus {
  return {
    methodId: "apv_live_file",
    label: "Live trace (apv_trace)",
    state: "running",
    detail: "/tmp/trace.jsonl",
    eventsReceived: 0,
    eventsIgnored: 0,
    ...overrides,
  };
}

function methodInfo(overrides: Partial<TraceMethodInfo> = {}): TraceMethodInfo {
  return {
    id: "apv_live_file",
    label: "Live trace (apv_trace)",
    description: "",
    warning: null,
    liveState: true,
    setupHint: null,
    settingsSchema: {},
    ...overrides,
  };
}

function setStore(connection: RuntimeConnectionState, stats: Partial<DeadLetterStats>) {
  useRuntimeStore.setState({ connection, deadLetterStats: { ...BASE_STATS, ...stats } });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("RuntimeStatusChip", () => {
  it("shows disconnected when the WebSocket isn't open, regardless of sources", async () => {
    const { fetchTraceMethods } = await import("../api");
    vi.mocked(fetchTraceMethods).mockResolvedValue([methodInfo()]);
    setStore("closed", { sources: [source({ eventsReceived: 10 })] });

    render(<RuntimeStatusChip />);

    expect(screen.getByText("Runtime: disconnected")).toBeInTheDocument();
  });

  it("shows off when connected but no trace source is configured", async () => {
    const { fetchTraceMethods } = await import("../api");
    vi.mocked(fetchTraceMethods).mockResolvedValue([]);
    setStore("open", { sources: [] });

    render(<RuntimeStatusChip />);

    expect(screen.getByText("Runtime: off")).toBeInTheDocument();
  });

  it("shows error when any source is in the error state", async () => {
    const { fetchTraceMethods } = await import("../api");
    vi.mocked(fetchTraceMethods).mockResolvedValue([methodInfo()]);
    setStore("open", {
      sources: [source({ state: "running" }), source({ methodId: "bad", state: "error", detail: "boom" })],
    });

    render(<RuntimeStatusChip />);

    expect(screen.getByText("Runtime: error")).toBeInTheDocument();
  });

  it("shows live with the event count once the running source's method reports liveState", async () => {
    const { fetchTraceMethods } = await import("../api");
    vi.mocked(fetchTraceMethods).mockResolvedValue([methodInfo({ liveState: true })]);
    setStore("open", { sources: [source({ state: "running", eventsReceived: 34 })] });

    render(<RuntimeStatusChip />);

    await waitFor(() => expect(screen.getByText("Runtime: live · 34 events")).toBeInTheDocument());
  });

  it("shows on (not live) when only after-the-fact methods are running", async () => {
    const { fetchTraceMethods } = await import("../api");
    vi.mocked(fetchTraceMethods).mockResolvedValue([methodInfo({ id: "otlp_http", liveState: false })]);
    setStore("open", {
      sources: [source({ methodId: "otlp_http", state: "running", eventsReceived: 5 })],
    });

    render(<RuntimeStatusChip />);

    await waitFor(() => expect(screen.getByText("Runtime: on · 5 events")).toBeInTheDocument());
  });
});
