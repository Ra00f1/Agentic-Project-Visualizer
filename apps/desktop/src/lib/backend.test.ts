/**
 * Task 20, Verification 10: `backend.ts` falls back to the VITE_* env vars
 * (or the backend's loopback default) outside Tauri.
 *
 * `@tauri-apps/api/core` is mocked here rather than left real: vitest runs
 * in jsdom, where `isTauri()` legitimately returns `false` and
 * `__TAURI_INTERNALS__` is legitimately absent, so the "outside Tauri" path
 * would be exercised either way -- but the "inside Tauri" branch (the
 * `invoke` call) needs a controllable double to test at all.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const invokeMock = vi.fn();
const isTauriMock = vi.fn(() => false);

vi.mock("@tauri-apps/api/core", () => ({
  invoke: invokeMock,
  isTauri: isTauriMock,
}));

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
  invokeMock.mockReset();
  isTauriMock.mockReset().mockReturnValue(false);
  Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
});

describe("getBackendInfo outside Tauri", () => {
  it("falls back to the VITE_* env vars when they're set", async () => {
    vi.stubEnv("VITE_APV_BACKEND_URL", "http://127.0.0.1:9999");
    vi.stubEnv("VITE_APV_SESSION_TOKEN", "test-token");
    const { getBackendInfo } = await import("./backend");

    const info = await getBackendInfo();

    expect(info).toEqual({ baseUrl: "http://127.0.0.1:9999", token: "test-token" });
    expect(invokeMock).not.toHaveBeenCalled();
  });

  it("falls back to the backend's own loopback default when unset", async () => {
    const { getBackendInfo } = await import("./backend");

    const info = await getBackendInfo();

    expect(info).toEqual({ baseUrl: "http://127.0.0.1:8765", token: null });
  });

  it("memoizes: a second call doesn't resolve the fallback again", async () => {
    const { getBackendInfo } = await import("./backend");

    const first = await getBackendInfo();
    vi.stubEnv("VITE_APV_BACKEND_URL", "http://127.0.0.1:1111");
    const second = await getBackendInfo();

    expect(second).toBe(first); // same object reference -- reused, not re-resolved
  });
});

describe("getBackendInfo inside Tauri", () => {
  it("invokes backend_info and returns its result", async () => {
    isTauriMock.mockReturnValue(true);
    invokeMock.mockResolvedValue({ baseUrl: "http://127.0.0.1:54321", token: "abc" });
    const { getBackendInfo } = await import("./backend");

    const info = await getBackendInfo();

    expect(invokeMock).toHaveBeenCalledWith("backend_info");
    expect(info).toEqual({ baseUrl: "http://127.0.0.1:54321", token: "abc" });
  });

  it("also counts as Tauri when only __TAURI_INTERNALS__ is present", async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    invokeMock.mockResolvedValue({ baseUrl: "http://127.0.0.1:1", token: "x" });
    const { getBackendInfo } = await import("./backend");

    await getBackendInfo();

    expect(invokeMock).toHaveBeenCalledWith("backend_info");
  });
});
