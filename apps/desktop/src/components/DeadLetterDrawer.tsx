/**
 * Toolbar button (badge = current dead-letter count) that opens a slide-over
 * panel listing recent trace events NodeIndex couldn't resolve.
 *
 * Hand-rolled, not a shadcn `Sheet` — shadcn/ui isn't set up in this repo
 * yet (no `components.json`, no Radix primitives installed; see Task 4's
 * `## Result` for why bringing in the whole design system for one drawer
 * was out of this task's scope). Same visual/functional shape as a Sheet
 * (backdrop + slide-over panel, Escape/backdrop-click to close) without the
 * dependency — matches `DetailPanel.tsx`'s existing Escape-to-close pattern.
 */

import { useEffect, useState } from "react";
import { fetchDeadLetterEntries } from "../api";
import { useRuntimeStore } from "../state/runtime";
import type { DeadLetterEntry } from "@shared-types/runtime";

export function DeadLetterDrawer() {
  const count = useRuntimeStore((s) => s.deadLetterStats.count);
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState<DeadLetterEntry[]>([]);
  const [status, setStatus] = useState<"idle" | "loading" | "error">("idle");
  const [error, setError] = useState<string | null>(null);

  // Fetch fresh entries each time the drawer opens rather than keeping a
  // live subscription -- dead letters are a debug aid, not something that
  // needs to visibly update while the panel is already open. Aborts the
  // in-flight request on a fast close/reopen or unmount, not just its
  // result -- toggling the toolbar button quickly would otherwise leave a
  // stale request running to completion for a response nothing ever uses.
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setStatus("loading");
    setError(null);
    void fetchDeadLetterEntries({ signal: controller.signal })
      .then((result) => {
        setEntries(result);
        setStatus("idle");
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
        setStatus("error");
      });
    return () => {
      controller.abort();
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="relative rounded border border-neutral-300 px-3 py-1 text-sm text-neutral-900 hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-100 dark:hover:bg-neutral-800"
        title="Trace events the runtime overlay couldn't match to a graph node"
      >
        Dead letters
        {count > 0 && (
          <span className="ml-1.5 inline-flex min-w-[1.25rem] items-center justify-center rounded-full bg-amber-500 px-1 text-[10px] font-bold leading-4 text-white">
            {count}
          </span>
        )}
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-label="Dead-letter events">
          {/* Backdrop — click to close, same convention as NodeContextMenu's outside-click dismiss. */}
          <button
            className="absolute inset-0 bg-black/30"
            aria-label="Close"
            onClick={() => setOpen(false)}
          />
          <aside className="relative flex h-full w-[420px] flex-col border-l border-neutral-200 bg-white shadow-xl dark:border-neutral-800 dark:bg-neutral-950">
            <header className="flex items-center justify-between border-b border-neutral-200 px-4 py-3 dark:border-neutral-800">
              <h2 className="text-sm font-semibold text-neutral-900 dark:text-neutral-50">
                Dead letters ({count})
              </h2>
              <button
                onClick={() => setOpen(false)}
                className="rounded p-1 text-neutral-500 hover:bg-neutral-100 hover:text-neutral-900 dark:text-neutral-400 dark:hover:bg-neutral-800 dark:hover:text-neutral-100"
                aria-label="Close"
                title="Close (Esc)"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M18 6L6 18M6 6l12 12" />
                </svg>
              </button>
            </header>

            <div className="flex-1 overflow-y-auto px-4 py-3">
              {status === "loading" && (
                <p className="text-xs text-neutral-500 dark:text-neutral-400">Loading…</p>
              )}
              {status === "error" && (
                <p className="text-xs text-red-600 dark:text-red-400">{error}</p>
              )}
              {status === "idle" && entries.length === 0 && (
                <p className="text-xs italic text-neutral-500 dark:text-neutral-400">
                  Nothing unresolved right now.
                </p>
              )}
              {entries.map((entry, i) => (
                <div
                  key={`${entry.recordedAt}-${i}`}
                  className="mb-2 rounded border border-neutral-200 bg-neutral-50 px-2 py-1.5 text-xs dark:border-neutral-800 dark:bg-neutral-900"
                >
                  <div className="mb-0.5 flex items-center justify-between">
                    <span className="font-mono font-medium text-neutral-800 dark:text-neutral-200">
                      {entry.kind} · {entry.refName}
                    </span>
                    <span className="text-[10px] text-neutral-500 dark:text-neutral-400">
                      {new Date(entry.recordedAt).toLocaleTimeString()}
                    </span>
                  </div>
                  <div className="mb-0.5 font-mono text-[11px] text-neutral-500 dark:text-neutral-400">
                    {entry.refModule}
                    {entry.refQualname ? `.${entry.refQualname}` : ""}
                  </div>
                  <div className="text-neutral-700 dark:text-neutral-300">{entry.reason}</div>
                </div>
              ))}
            </div>
          </aside>
        </div>
      )}
    </>
  );
}
