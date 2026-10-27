/**
 * Task 20, Verification 12: `useRuntimeChannel` builds the WS URL from
 * `getBackendInfo()` and offers the `apv.v1`/`apv.token.<hex>` subprotocols
 * when a token is present (the server-side half of this is
 * `test_security.py`'s WS tests).
 *
 * A fake `WebSocket` class stands in for the real one -- jsdom has no
 * WebSocket server to connect to, and the point of this test is only
 * "what did the hook pass to the constructor", not real socket behavior
 * (that's covered by this hook's existing manual/integration testing via
 * the running app).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, cleanup } from "@testing-library/react";
import { useRuntimeChannel } from "./runtimeChannel";
import type { BackendInfo } from "./backend";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public url: string, public protocols?: string[]) {
    FakeWebSocket.instances.push(this);
  }

  close(): void {
    this.onclose?.();
  }
}

afterEach(() => {
  cleanup();
  FakeWebSocket.instances = [];
  vi.unstubAllGlobals();
});

describe("useRuntimeChannel", () => {
  it("offers the token subprotocols when a token is present", async () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const backend: BackendInfo = { baseUrl: "http://127.0.0.1:54321", token: "abc123" };

    renderHook(() => useRuntimeChannel(backend));
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    const socket = FakeWebSocket.instances[0]!;
    expect(socket.url).toBe("ws://127.0.0.1:54321/ws/runtime");
    expect(socket.protocols).toEqual(["apv.v1", "apv.token.abc123"]);
  });

  it("connects with no subprotocols when the token is null", async () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const backend: BackendInfo = { baseUrl: "http://127.0.0.1:54321", token: null };

    renderHook(() => useRuntimeChannel(backend));
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    expect(FakeWebSocket.instances[0]!.protocols).toBeUndefined();
  });

  it("closes the socket on unmount without scheduling a reconnect", async () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const backend: BackendInfo = { baseUrl: "http://127.0.0.1:54321", token: null };

    const { unmount } = renderHook(() => useRuntimeChannel(backend));
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    unmount();

    expect(FakeWebSocket.instances).toHaveLength(1); // no reconnect attempt after unmount
  });
});
