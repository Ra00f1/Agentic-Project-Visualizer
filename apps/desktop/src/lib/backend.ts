/**
 * Resolves where the Python backend lives and the token to authenticate
 * with it.
 *
 * Inside Tauri, the Rust shell has already spawned the backend and read
 * its handshake (see `src-tauri/src/backend.rs`); this just asks for the
 * result via the `backend_info` command. Outside Tauri — `vite`/`vitest`
 * in a plain browser, or a component test — there's no shell to ask, so
 * this falls back to `VITE_APV_BACKEND_URL` / `VITE_APV_SESSION_TOKEN` (or
 * the backend's own loopback default) for manual dev work against a
 * hand-started `uvicorn`.
 *
 * Memoized: Rust's `BackendState` reports one outcome for the process's
 * whole lifetime (one backend is spawned at startup), so repeated callers
 * should share the same promise instead of re-invoking the command.
 */

import { invoke, isTauri } from "@tauri-apps/api/core";

export interface BackendInfo {
  baseUrl: string;
  token: string | null;
}

let cached: Promise<BackendInfo> | null = null;

// `isTauri()` is the documented check in the installed @tauri-apps/api
// (2.11.1). `__TAURI_INTERNALS__` is the lower-level global it reads
// internally — kept as a fallback per this task's own Design, in case a
// future API change makes `isTauri()` itself unreliable.
function runningInsideTauri(): boolean {
  if (isTauri()) return true;
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function fallbackBackendInfo(): BackendInfo {
  return {
    baseUrl: import.meta.env.VITE_APV_BACKEND_URL ?? "http://127.0.0.1:8765",
    token: import.meta.env.VITE_APV_SESSION_TOKEN ?? null,
  };
}

export function getBackendInfo(): Promise<BackendInfo> {
  if (!cached) {
    cached = runningInsideTauri()
      ? invoke<BackendInfo>("backend_info")
      : Promise.resolve(fallbackBackendInfo());
  }
  return cached;
}

/** Test-only: clears the memoized promise so each test starts from a clean
 *  slate instead of reusing whatever the previous test resolved. */
export function _resetBackendInfoCacheForTests(): void {
  cached = null;
}
