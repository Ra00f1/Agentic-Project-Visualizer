/**
 * Error-toast queue — separate from `useRuntimeStore` on purpose.
 *
 * Not in Task 4's literal Zustand-slice spec (`states`, `deadLetterStats`,
 * four actions), but the task also asks for "error toast on the first
 * errored transition per node per session" — that needs somewhere to track
 * "have we already toasted this node" and a small queue of pending toasts,
 * neither of which belongs in the runtime-state slice itself (that slice
 * is a pure projection of the backend's state; toast bookkeeping is
 * frontend-only UI concern). Kept as its own tiny store so `useRuntimeStore`
 * stays exactly what the task specifies.
 */

import { create } from "zustand";
import type { RuntimeErrorInfo } from "@shared-types/runtime";

export interface RuntimeErrorToast {
  id: string;
  nodeId: string;
  error: RuntimeErrorInfo;
}

interface ToastStore {
  toasts: RuntimeErrorToast[];
  /** Node ids that have already produced a toast this session. Never
   *  cleared by `dismiss` or graph reset -- "per session" means once,
   *  full stop, even if the node recovers and errors again later. */
  toastedNodeIds: Set<string>;
  /** Push a toast for `nodeId`'s error, unless this node already toasted
   *  this session. Returns true if a toast was actually queued. */
  notifyError: (nodeId: string, error: RuntimeErrorInfo) => boolean;
  dismiss: (id: string) => void;
}

export const useRuntimeToastStore = create<ToastStore>((set, get) => ({
  toasts: [],
  toastedNodeIds: new Set(),

  notifyError: (nodeId, error) => {
    if (get().toastedNodeIds.has(nodeId)) return false;
    const id = `${nodeId}-${crypto.randomUUID()}`;
    set((prev) => ({
      toasts: [...prev.toasts, { id, nodeId, error }],
      toastedNodeIds: new Set(prev.toastedNodeIds).add(nodeId),
    }));
    return true;
  },

  dismiss: (id) => set((prev) => ({ toasts: prev.toasts.filter((t) => t.id !== id) })),
}));
