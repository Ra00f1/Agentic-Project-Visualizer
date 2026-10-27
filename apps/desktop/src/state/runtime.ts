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
  SourceStatus,
} from "@shared-types/runtime";

export interface NodeRuntimeState {
  status: NodeStatus;
  runCount: number;
  errorCount: number;
  lastError: RuntimeErrorInfo | null;
  lastEndedAt: string | null;
  /** Task 23's "just ran" flash. Bumped by `applyDelta` each time
   *  `lastEndedAt` actually CHANGES AND the node wasn't already shown
   *  `active` just before this delta (Task 24, D2 — a span this client
   *  watched glow live doesn't also flash the instant it finishes; the
   *  flash is for a completion that was never shown live at all, e.g.
   *  every OTLP delta, or a live span so short its start and end landed in
   *  the same batch window). Never bumped by `applySnapshot` — a fresh
   *  connect must not flash every node. `EntityNode` keys its flash
   *  overlay `<span>` on this value: a changed key restarts the CSS
   *  animation with no timers needed (see `lib/runtime-overlay.css`'s
   *  `apv-node-ran`). Only the fact that it changed matters, never the
   *  number itself — it never resets to 0. */
  flashSeq: number;
}

/** Mirrors `DeadLetterStatsMessage` (minus its `type` discriminator) --
 *  kept as its OWN interface (not a reused/derived type) so the store's
 *  field names (`dropped`, `parseErrors`) can stay the short, established
 *  names `DeadLetterDrawer.tsx` already used, while still keeping every
 *  field the wire message carries (Task 21 Fix 3 -- audit #09 flagged
 *  `nonTraceCount` getting silently dropped here before this).
 *
 *  `sources` (Task 22) replaces the single-source `sourcePath`/
 *  `sourceState`/`eventsReceived` fields -- the backend can run several
 *  trace sources at once now. See `components/RuntimeStatusChip.tsx` for
 *  how it's summarized. */
export interface DeadLetterStats {
  count: number;
  dropped: number;
  parseErrors: number;
  nonTrace: number;
  eventsResolved: number;
  sources: SourceStatus[];
}

/** WebSocket connection state, written by `useRuntimeChannel` (Task 21 Fix
 *  3) so the UI can tell "disconnected" apart from "connected but the
 *  overlay is off" -- both used to render identically. */
export type RuntimeConnectionState = "connecting" | "open" | "closed";

export interface RuntimeStore {
  states: Record<string, NodeRuntimeState>;
  deadLetterStats: DeadLetterStats;
  connection: RuntimeConnectionState;
  applyDelta: (message: RuntimeDeltaMessage) => void;
  applySnapshot: (message: RuntimeSnapshotMessage) => void;
  applyReset: () => void;
  applyDeadLetterStats: (message: DeadLetterStatsMessage) => void;
  setConnection: (connection: RuntimeConnectionState) => void;
}

const INITIAL_DEAD_LETTER_STATS: DeadLetterStats = {
  count: 0,
  dropped: 0,
  parseErrors: 0,
  nonTrace: 0,
  eventsResolved: 0,
  sources: [],
};

export const useRuntimeStore = create<RuntimeStore>((set) => ({
  states: {},
  deadLetterStats: INITIAL_DEAD_LETTER_STATS,
  connection: "connecting",

  applyDelta: (message) =>
    set((prev) => {
      const previous = prev.states[message.nodeId];
      // Task 24 (D2): the flash is now only for a run that was NEVER shown
      // live -- `lastEndedAt` genuinely changing (not just non-null) is
      // still required (a repeat delta with the same `lastEndedAt` must not
      // re-flash), but it's no longer sufficient on its own: a span that
      // was visibly `active` before this delta already got its "running"
      // signal from the glow (`.apv-node-active`, bright from its first
      // frame as of this same task) -- flashing it AGAIN the instant it
      // goes idle was the dominant visual effect Step 0 found (the flash
      // starts at full brightness; the glow barely got going before a
      // 0.2-0.4s step ended). OTLP's always-after-the-fact deltas, and a
      // live span so short its start and end landed in the same batch
      // window, never show `previous.status === "active"` here -- they
      // still flash, which is the point: it's their only signal.
      const justFinished =
        message.lastEndedAt !== null &&
        message.lastEndedAt !== previous?.lastEndedAt &&
        previous?.status !== "active";
      return {
        states: {
          ...prev.states,
          [message.nodeId]: {
            status: message.status,
            runCount: message.runCount,
            errorCount: message.errorCount,
            lastError: message.lastError,
            lastEndedAt: message.lastEndedAt,
            flashSeq: (previous?.flashSeq ?? 0) + (justFinished ? 1 : 0),
          },
        },
      };
    }),

  applySnapshot: (message) => {
    const states: Record<string, NodeRuntimeState> = {};
    for (const entry of message.states) {
      states[entry.nodeId] = {
        status: entry.status,
        runCount: entry.runCount,
        errorCount: entry.errorCount,
        lastError: entry.lastError,
        lastEndedAt: entry.lastEndedAt,
        flashSeq: 0, // never animate on a fresh connect -- see flashSeq's own docstring.
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
        nonTrace: message.nonTraceCount,
        eventsResolved: message.eventsResolved,
        sources: message.sources,
      },
    }),

  setConnection: (connection) => set({ connection }),
}));
