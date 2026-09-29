/**
 * Shared runtime-overlay WebSocket types — mirror the Pydantic models in
 * `apps/backend/src/agentic_visualizer/runtime/messages.py`.
 *
 * Hand-rolled, same as `graph.ts` (see that file's own docstring for why: a
 * real generator is explicitly deferred there, and this file follows the
 * same "day one" convention rather than introducing a second approach for
 * one more set of types). If/when `apps/backend/scripts/export_trace_schema.py`-style
 * generation grows to cover WS messages too, this file becomes its output
 * target.
 *
 * `RuntimeMessage` is a discriminated union on `type` — exactly what
 * `/ws/runtime` sends, message by message. Do NOT add transformation logic
 * here; see `graph.ts`'s docstring for why.
 */

/** One node's live status, derived from resolved trace events. */
export type NodeStatus = "idle" | "active" | "errored";

/** Detail for a node's most recent error. Mirrors `apv_trace`'s `meta.error`
 *  shape (type/message/traceback) plus the event's own timestamp. */
export interface RuntimeErrorInfo {
  type: string;
  message: string;
  traceback: string;
  /** ISO-8601 string — see `Provenance.scannedAt` in graph.ts for why this
   *  stays a string rather than `Date` here too. */
  at: string;
}

/** One node's runtime state — the per-node shape shared by delta and snapshot. */
export interface NodeStateEntry {
  nodeId: string;
  status: NodeStatus;
  runCount: number;
  errorCount: number;
  lastError: RuntimeErrorInfo | null;
}

/** One node's state changed. */
export interface RuntimeDeltaMessage {
  type: "delta";
  nodeId: string;
  status: NodeStatus;
  runCount: number;
  errorCount: number;
  lastError: RuntimeErrorInfo | null;
}

/** Sent once, right after connect: every currently-tracked node's state. */
export interface RuntimeSnapshotMessage {
  type: "snapshot";
  states: NodeStateEntry[];
}

/** Sent periodically (every 2s). `count` is the dead-letter buffer's own
 *  size; `droppedCount`/`parseErrorCount` come from the active TraceSource
 *  (the file-tailing layer, a different concern from resolution). */
export interface DeadLetterStatsMessage {
  type: "dead_letter_stats";
  count: number;
  droppedCount: number;
  parseErrorCount: number;
}

/** Sent when the store resets — a fresh graph swapped in, all prior state is stale. */
export interface RuntimeResetMessage {
  type: "reset";
}

/** Every message shape `/ws/runtime` can send. Narrow on `type` to discriminate. */
export type RuntimeMessage =
  | RuntimeDeltaMessage
  | RuntimeSnapshotMessage
  | DeadLetterStatsMessage
  | RuntimeResetMessage;

/** One event NodeIndex couldn't resolve. Mirrors the backend's
 *  `DeadLetterEntry` (runtime/dead_letter.py), served by `GET /runtime/dead-letter`
 *  (added in Task 4 -- Task 3's DeadLetterBuffer had no REST endpoint yet). */
export interface DeadLetterEntry {
  kind: string;
  refName: string;
  refModule: string;
  refQualname: string | null;
  phase: string;
  reason: string;
  /** ISO-8601 string — see `Provenance.scannedAt` in graph.ts for why this
   *  stays a string rather than `Date` here too. */
  recordedAt: string;
}
