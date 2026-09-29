/**
 * Top-right dismissible toasts — one per node's FIRST `errored` transition
 * this session (the one-per-node-ever gate lives in `useRuntimeToastStore`;
 * see that file for why "debounced 2s" from Task 4's Design section reads
 * as "don't need a debounce timer here" once a node can only ever queue
 * one toast in the first place).
 *
 * Hand-rolled, not shadcn's `Toaster` — same reasoning as `DeadLetterDrawer`:
 * shadcn/ui isn't set up in this repo yet, and this component's needs
 * (auto-dismiss, manual dismiss, stack top-right) don't need the dependency.
 */

import { useEffect, useRef } from "react";
import { useRuntimeToastStore } from "../state/runtimeToasts";

const AUTO_DISMISS_MS = 8000;

export function RuntimeErrorToasts() {
  const toasts = useRuntimeToastStore((s) => s.toasts);
  const dismiss = useRuntimeToastStore((s) => s.dismiss);
  // Maps toast id -> its scheduled timer -- without this, the effect below
  // would re-schedule (and effectively reset) every toast's timer whenever
  // ANY toast is added or removed, since `toasts` is a new array reference
  // each time. Keeping the timer handle (not just a seen-id marker) also
  // lets the effect's cleanup actually clear pending timers on unmount,
  // rather than leaving them to fire later against an unmounted component.
  const scheduledRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  useEffect(() => {
    for (const toast of toasts) {
      if (scheduledRef.current.has(toast.id)) continue;
      const timer = setTimeout(() => {
        dismiss(toast.id);
        scheduledRef.current.delete(toast.id);
      }, AUTO_DISMISS_MS);
      scheduledRef.current.set(toast.id, timer);
    }
    // No cleanup here: this effect reruns on every toast add/remove (since
    // `toasts` is a new array reference each time), and a cleanup returned
    // from it would fire on every one of those reruns, not just unmount --
    // clearing every OTHER toast's still-pending timer and forcing a fresh
    // scheduledRef entry on the next render would restart their countdowns
    // from zero instead of letting them keep counting down. Unmount-only
    // teardown lives in the effect below, which never reruns.
  }, [toasts, dismiss]);

  useEffect(() => {
    const scheduled = scheduledRef.current;
    return () => {
      for (const timer of scheduled.values()) clearTimeout(timer);
      scheduled.clear();
    };
  }, []);

  if (toasts.length === 0) return null;

  return (
    <div className="fixed right-4 top-4 z-[60] flex w-80 flex-col gap-2" aria-live="polite">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          role="alert"
          className="rounded border border-red-300 bg-red-50 px-3 py-2 text-xs shadow-lg dark:border-red-900 dark:bg-red-950"
        >
          <div className="mb-0.5 flex items-start justify-between gap-2">
            <span className="font-semibold text-red-800 dark:text-red-300">{toast.error.type}</span>
            <button
              onClick={() => dismiss(toast.id)}
              className="shrink-0 text-red-500 hover:text-red-700 dark:text-red-400 dark:hover:text-red-200"
              aria-label="Dismiss"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            </button>
          </div>
          <div className="mb-0.5 break-words text-red-700 dark:text-red-300">{toast.error.message}</div>
          <div className="truncate font-mono text-[10px] text-red-500 dark:text-red-400" title={toast.nodeId}>
            {toast.nodeId}
          </div>
        </div>
      ))}
    </div>
  );
}
