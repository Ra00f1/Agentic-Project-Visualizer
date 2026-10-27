/**
 * `useRuntimeChannel` — connects to `/ws/runtime`, dispatches every message
 * into `useRuntimeStore` (and error deltas into the toast queue).
 *
 * Lives in `src/lib/` (not a new `src/api/` directory): the app already has
 * a single `src/api.ts` FILE for the HTTP client, and a directory can't
 * share that name. `src/lib/` already holds another hook in this exact
 * shape (`useTheme.ts`), so this follows that precedent instead of
 * inventing a new top-level folder for one file.
 *
 * Auth: the WS URL and token come from `getBackendInfo()` (same source as
 * the REST client). Browsers can't set custom headers on a WebSocket
 * handshake, so the token travels as a subprotocol instead --
 * `["apv.v1", "apv.token.<hex>"]` -- which the backend's `/ws/runtime`
 * checks before accepting (see `security.py`). Resolved once per connect
 * attempt rather than once per hook lifetime, since a reconnect after the
 * backend restarted should pick up a fresh token rather than keep retrying
 * a stale one.
 *
 * Reconnect: exponential backoff 500ms → 1s → 2s → 5s → 15s → 30s (capped),
 * reset to the first step on a successful connect. On reconnect the backend
 * sends a fresh snapshot (per its own `/ws/runtime` handler), which
 * `applySnapshot` replaces wholesale -- no diffing needed here.
 *
 * Connection state (Task 21 Fix 3): every (re)connect attempt writes
 * `"connecting"` to the store before it does anything else, `onopen` writes
 * `"open"`, `onclose` writes `"closed"`. `RuntimeStatusChip` reads this to
 * show "disconnected" instead of silently reusing whatever `sources` list
 * the last `dead_letter_stats` message carried, which would otherwise look
 * identical to "connected and the overlay happens to be off."
 */

import { useEffect, useRef } from "react";
import type { RuntimeMessage } from "@shared-types/runtime";
import { getBackendInfo, type BackendInfo } from "./backend";
import { useRuntimeStore } from "../state/runtime";
import { useRuntimeToastStore } from "../state/runtimeToasts";

const BACKOFF_STEPS_MS = [500, 1000, 2000, 5000, 15000, 30000];

function wsUrlFor(baseUrl: string): string {
  // http(s) -> ws(s), same host/port as the REST API.
  const url = new URL(baseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws/runtime";
  return url.toString();
}

function handleMessage(message: RuntimeMessage): void {
  switch (message.type) {
    case "snapshot":
      useRuntimeStore.getState().applySnapshot(message);
      return;
    case "delta":
      useRuntimeStore.getState().applyDelta(message);
      if (message.status === "errored" && message.lastError) {
        useRuntimeToastStore.getState().notifyError(message.nodeId, message.lastError);
      }
      return;
    case "reset":
      useRuntimeStore.getState().applyReset();
      return;
    case "dead_letter_stats":
      useRuntimeStore.getState().applyDeadLetterStats(message);
      return;
  }
}

/** Connects on mount, reconnects with backoff on drop, disconnects on unmount.
 *  Call once, at the app root (mirrors `useTheme`'s single-call-site pattern).
 *  `backend` is test-only — production callers let it resolve via
 *  `getBackendInfo()` on every (re)connect, so a backend restart mid-session
 *  (and its fresh token) is picked up rather than retried with a stale one. */
export function useRuntimeChannel(backend?: BackendInfo): void {
  const attemptRef = useRef(0);
  const unmountedRef = useRef(false);

  useEffect(() => {
    unmountedRef.current = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;

    async function connect(): Promise<void> {
      // Set BEFORE resolving backend info / opening the socket, not just on
      // the eventual onopen -- a slow getBackendInfo() call or a long
      // backoff wait should both show as "connecting," not linger on
      // whatever the previous attempt's state was (Task 21 Fix 3).
      useRuntimeStore.getState().setConnection("connecting");
      const info = backend ?? (await getBackendInfo());
      if (unmountedRef.current) return; // unmounted while awaiting backend info

      const protocols = info.token ? ["apv.v1", `apv.token.${info.token}`] : undefined;
      socket = new WebSocket(wsUrlFor(info.baseUrl), protocols);

      socket.onopen = () => {
        attemptRef.current = 0;
        useRuntimeStore.getState().setConnection("open");
      };

      socket.onmessage = (event) => {
        let message: RuntimeMessage;
        try {
          message = JSON.parse(event.data as string) as RuntimeMessage;
        } catch {
          return; // Malformed frame -- ignore rather than crash the app.
        }
        handleMessage(message);
      };

      socket.onclose = () => {
        useRuntimeStore.getState().setConnection("closed");
        if (unmountedRef.current) return;
        scheduleReconnect();
      };

      // A WebSocket's `error` event carries no useful detail and is always
      // followed by `close` -- reconnect scheduling lives in onclose only,
      // to avoid double-scheduling from both handlers firing.
      socket.onerror = () => {
        socket?.close();
      };
    }

    function scheduleReconnect(): void {
      const index = Math.min(attemptRef.current, BACKOFF_STEPS_MS.length - 1);
      const delay = BACKOFF_STEPS_MS[index]!;
      attemptRef.current += 1;
      reconnectTimer = setTimeout(() => void connect(), delay);
    }

    void connect();

    return () => {
      unmountedRef.current = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [backend]);
}
