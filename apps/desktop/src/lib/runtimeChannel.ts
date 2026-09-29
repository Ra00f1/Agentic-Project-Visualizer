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
 * Reconnect: exponential backoff 500ms → 1s → 2s → 5s → 15s → 30s (capped),
 * reset to the first step on a successful connect. On reconnect the backend
 * sends a fresh snapshot (per its own `/ws/runtime` handler), which
 * `applySnapshot` replaces wholesale -- no diffing needed here.
 */

import { useEffect, useRef } from "react";
import type { RuntimeMessage } from "@shared-types/runtime";
import { DEFAULT_BASE_URL } from "../api";
import { useRuntimeStore } from "../state/runtime";
import { useRuntimeToastStore } from "../state/runtimeToasts";

const BACKOFF_STEPS_MS = [500, 1000, 2000, 5000, 15000, 30000];

function defaultWsUrl(): string {
  // http(s) -> ws(s), same host/port as the REST API.
  const url = new URL(DEFAULT_BASE_URL);
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
 *  Call once, at the app root (mirrors `useTheme`'s single-call-site pattern). */
export function useRuntimeChannel(wsUrl: string = defaultWsUrl()): void {
  const attemptRef = useRef(0);
  const unmountedRef = useRef(false);

  useEffect(() => {
    unmountedRef.current = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;

    function connect(): void {
      socket = new WebSocket(wsUrl);

      socket.onopen = () => {
        attemptRef.current = 0;
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
      reconnectTimer = setTimeout(connect, delay);
    }

    connect();

    return () => {
      unmountedRef.current = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [wsUrl]);
}
