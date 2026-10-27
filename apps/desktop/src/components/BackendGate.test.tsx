/**
 * Task 20, Verification 13: BackendGate renders its loading and error
 * states (the "ready" state is implicit -- it's just `children`, already
 * covered by every other component test that renders through it).
 *
 * Task 22 adds coverage for the "re-apply the saved runtime-tracing
 * config once the backend is ready" side effect.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { BackendGate } from "./BackendGate";
import { RUNTIME_METHODS_STORAGE_KEY } from "./setup/TracingMethods";

vi.mock("../lib/backend", () => ({
  getBackendInfo: vi.fn(),
}));

vi.mock("../api", () => ({
  putRuntimeConfig: vi.fn().mockResolvedValue({ methods: [], sources: [] }),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  localStorage.clear();
});

describe("BackendGate", () => {
  it("shows the loading state before getBackendInfo resolves", async () => {
    const { getBackendInfo } = await import("../lib/backend");
    vi.mocked(getBackendInfo).mockReturnValue(new Promise(() => {})); // never resolves

    render(
      <BackendGate>
        <div>app content</div>
      </BackendGate>,
    );

    expect(screen.getByText("Starting backend…")).toBeInTheDocument();
    expect(screen.queryByText("app content")).not.toBeInTheDocument();
  });

  it("renders children once getBackendInfo resolves", async () => {
    const { getBackendInfo } = await import("../lib/backend");
    vi.mocked(getBackendInfo).mockResolvedValue({ baseUrl: "http://127.0.0.1:1", token: "t" });

    render(
      <BackendGate>
        <div>app content</div>
      </BackendGate>,
    );

    await waitFor(() => expect(screen.getByText("app content")).toBeInTheDocument());
  });

  it("shows the error panel with the failure message when getBackendInfo rejects", async () => {
    const { getBackendInfo } = await import("../lib/backend");
    vi.mocked(getBackendInfo).mockRejectedValue(new Error("bundled backend not found at C:\\app\\backend\\apv-backend.exe"));

    render(
      <BackendGate>
        <div>app content</div>
      </BackendGate>,
    );

    await waitFor(() => expect(screen.getByText("The backend couldn't start.")).toBeInTheDocument());
    expect(screen.getByText("bundled backend not found at C:\\app\\backend\\apv-backend.exe")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close and reopen the app" })).toBeInTheDocument();
    expect(screen.queryByText("app content")).not.toBeInTheDocument();
  });

  it("PUTs the saved runtime-tracing config once the backend is ready", async () => {
    localStorage.setItem(
      RUNTIME_METHODS_STORAGE_KEY,
      JSON.stringify([{ id: "apv_live_file", settings: { path: "saved.jsonl" } }]),
    );
    const { getBackendInfo } = await import("../lib/backend");
    vi.mocked(getBackendInfo).mockResolvedValue({ baseUrl: "http://127.0.0.1:1", token: "t" });
    const { putRuntimeConfig } = await import("../api");

    render(
      <BackendGate>
        <div>app content</div>
      </BackendGate>,
    );

    await waitFor(() =>
      expect(putRuntimeConfig).toHaveBeenCalledWith({
        methods: [{ id: "apv_live_file", settings: { path: "saved.jsonl" } }],
      }),
    );
  });

  it("does not PUT anything when nothing was ever saved", async () => {
    const { getBackendInfo } = await import("../lib/backend");
    vi.mocked(getBackendInfo).mockResolvedValue({ baseUrl: "http://127.0.0.1:1", token: "t" });
    const { putRuntimeConfig } = await import("../api");

    render(
      <BackendGate>
        <div>app content</div>
      </BackendGate>,
    );

    await waitFor(() => expect(screen.getByText("app content")).toBeInTheDocument());
    expect(putRuntimeConfig).not.toHaveBeenCalled();
  });
});
