/**
 * Task 20, Verification 11: the `request` helper (exercised here through
 * `fetchDeadLetterEntries`, the simplest caller) adds the bearer header
 * when a token is present, omits it when not, and turns a 401 into the
 * fixed "backend session mismatch" error rather than surfacing it as a
 * generic HTTP failure.
 *
 * Every test supplies its own `backend` param (bypassing `getBackendInfo`/
 * Tauri detection entirely) and stubs `global.fetch` directly -- this file
 * is about the header/URL-building logic in `api.ts`, not about backend
 * resolution (that's `lib/backend.test.ts`'s job).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchDeadLetterEntries } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("request (via fetchDeadLetterEntries)", () => {
  it("adds the bearer header when a token is present", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await fetchDeadLetterEntries({ backend: { baseUrl: "http://127.0.0.1:8765", token: "secret" } });

    const [, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer secret");
  });

  it("omits the header when the token is null", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await fetchDeadLetterEntries({ backend: { baseUrl: "http://127.0.0.1:8765", token: null } });

    const [, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("throws a fixed message on 401 instead of surfacing it as a generic failure", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("", { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      fetchDeadLetterEntries({ backend: { baseUrl: "http://127.0.0.1:8765", token: "wrong" } }),
    ).rejects.toThrow("backend session mismatch, restart the app");
  });
});
