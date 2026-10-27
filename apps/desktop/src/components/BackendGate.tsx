/**
 * Wraps the app; nothing underneath renders until the backend is reachable.
 *
 * `getBackendInfo()` resolves once the Rust shell's handshake with the
 * Python backend completes (or rejects with the Rust-side failure message
 * — see `src-tauri/src/backend.rs::run_backend`, whose error strings
 * already carry the relevant detail: the spawn error, the stderr tail plus
 * `backend.log`'s path, or "bundled backend not found at <path>"). Outside
 * Tauri this resolves immediately via the VITE_* fallback (see
 * `lib/backend.ts`), so the gate is invisible in that path.
 *
 * Deliberately NOT retried automatically: a backend that failed to start
 * (missing `uv`, a port/firewall issue, a missing bundled binary) won't
 * usually fix itself by polling, and D2/D1's design puts a human in the
 * loop here — the only recovery path is to look at the log and relaunch.
 */

import { useEffect, useState, type ReactNode } from "react";
import { putRuntimeConfig } from "../api";
import { getBackendInfo } from "../lib/backend";
import { loadSavedTracingEntries } from "./setup/TracingMethods";

type GateState = { status: "loading" } | { status: "ready" } | { status: "error"; message: string };

/** Task 22: "App start: once the backend is ready, if apv.runtime.methods
 *  exists, PUT it." A fresh backend process has no memory of what was
 *  checked on Setup last time -- only `AGENTIC_DEFAULT_TRACE_LOG_PATH`'s
 *  env-var default, if any, survives a restart on its own. Fire-and-forget:
 *  this must not hold up rendering the app, and a failure here (backend
 *  briefly unreachable, a method that no longer exists) isn't worth its
 *  own error screen -- `RuntimeStatusChip` already surfaces whatever state
 *  the backend ends up in. Does nothing when nothing was ever saved,
 *  leaving the env-var default (if the backend was started with one)
 *  exactly as `_lifespan` set it up. */
function reapplySavedTracingConfig(): void {
  const saved = loadSavedTracingEntries();
  if (saved === null) return;
  putRuntimeConfig({ methods: saved }).catch(() => {
    // Non-fatal -- see this function's own docstring.
  });
}

async function closeWindow(): Promise<void> {
  // Dynamic import: this module reaches into Tauri's window API, which has
  // nothing to do outside Tauri. The button that calls this only renders
  // inside the app, but importing lazily keeps that assumption from being
  // load-bearing.
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  await getCurrentWindow().close();
}

export function BackendGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<GateState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    getBackendInfo()
      .then(() => {
        if (!cancelled) {
          setState({ status: "ready" });
          reapplySavedTracingConfig();
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.status === "ready") return <>{children}</>;

  if (state.status === "loading") {
    return (
      <div className="flex h-screen w-screen flex-col items-center justify-center gap-3 bg-white text-neutral-800 dark:bg-neutral-950 dark:text-neutral-200">
        <div
          role="status"
          aria-label="Starting backend"
          className="h-6 w-6 animate-spin rounded-full border-2 border-neutral-300 border-t-neutral-900 dark:border-neutral-700 dark:border-t-neutral-100"
        />
        <p className="text-sm text-neutral-600 dark:text-neutral-400">Starting backend…</p>
      </div>
    );
  }

  return (
    <div className="flex h-screen w-screen flex-col items-center justify-center gap-3 bg-white p-8 text-center text-neutral-800 dark:bg-neutral-950 dark:text-neutral-200">
      <div className="rounded-full bg-red-100 p-3 text-red-700 dark:bg-red-950 dark:text-red-300">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 9v4M12 17h.01M4.93 19h14.14a2 2 0 0 0 1.72-3L13.72 4.87a2 2 0 0 0-3.44 0L3.21 16a2 2 0 0 0 1.72 3z" />
        </svg>
      </div>
      <h1 className="text-lg font-semibold">The backend couldn't start.</h1>
      <pre className="max-w-lg overflow-auto rounded border border-neutral-200 bg-neutral-50 p-3 text-left font-mono text-[11px] text-neutral-800 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-200">
        {state.message}
      </pre>
      <p className="text-xs text-neutral-500 dark:text-neutral-400">
        Full output, including this run's log, is in backend.log in the app's log folder.
      </p>
      <button
        onClick={() => void closeWindow()}
        className="rounded bg-neutral-900 px-4 py-1.5 text-sm text-white dark:bg-neutral-100 dark:text-neutral-900"
      >
        Close and reopen the app
      </button>
    </div>
  );
}
