/**
 * Runtime-overlay state — one Zustand slice, updated by `/ws/runtime` messages.
 *
 * Deliberately NOT part of React Flow's node `data`. React Flow diffs nodes
 * by their `data` prop; if runtime status lived there, every delta would
 * trigger React Flow to rebuild that node's DOM, cascading into an edge
 * recompute. Instead, `EntityNode` subscribes directly to this store for
 * its own id — the slice update is O(1), only that node's function
 * component re-renders, React Flow's diff sees no `data` change, and edges
 * stay put. See Task 4's own Design section for the full reasoning.
 */

import { create } from "zustand";
import type {
  DeadLetterStatsMessage,
  NodeStatus,
  RuntimeDeltaMessage,
  RuntimeErrorInfo,
  RuntimeSnapshotMessage,
} from "@shared-types/runtime";

export interface NodeRuntimeState {
  status: NodeStatus;
  runCount: number;
  errorCount: number;
  lastError: RuntimeErrorInfo | null;
}

export interface DeadLetterStats {
  count: number;
  dropped: number;
  parseErrors: number;
}

export interface RuntimeStore {
  states: Record<string, NodeRuntimeState>;
  deadLetterStats: DeadLetterStats;
  applyDelta: (message: RuntimeDeltaMessage) => void;
  applySnapshot: (message: RuntimeSnapshotMessage) => void;
  applyReset: () => void;
  applyDeadLetterStats: (message: DeadLetterStatsMessage) => void;
}

const INITIAL_DEAD_LETTER_STATS: DeadLetterStats = { count: 0, dropped: 0, parseErrors: 0 };

function stateFromDelta(message: RuntimeDeltaMessage): NodeRuntimeState {
  return {
    status: message.status,
    runCount: message.runCount,
    errorCount: message.errorCount,
    lastError: message.lastError,
  };
}

export const useRuntimeStore = create<RuntimeStore>((set) => ({
  states: {},
  deadLetterStats: INITIAL_DEAD_LETTER_STATS,

  applyDelta: (message) =>
    set((prev) => ({
      states: { ...prev.states, [message.nodeId]: stateFromDelta(message) },
    })),

  applySnapshot: (message) => {
    const states: Record<string, NodeRuntimeState> = {};
    for (const entry of message.states) {
      states[entry.nodeId] = {
        status: entry.status,
        runCount: entry.runCount,
        errorCount: entry.errorCount,
        lastError: entry.lastError,
      };
    }
    set({ states });
  },

  // Same treatment as dead-letter counts (Task 4's own Design note): the
  // graph refreshed, prior runtime state no longer describes anything —
  // nodes return to idle (the default for an id with no entry) until
  // fresh events arrive.
  applyReset: () => set({ states: {} }),

  applyDeadLetterStats: (message) =>
    set({
      deadLetterStats: {
        count: message.count,
        dropped: message.droppedCount,
        parseErrors: message.parseErrorCount,
      },
    }),
}));
