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
  /** When this node's most recent `end`/`error` span finished (Task 23's
   *  "just ran" flash) -- ISO-8601 string, or null until the node has ever
   *  finished a span. See `state/runtime.ts`'s `flashSeq` for how this
   *  drives the flash without caring about the absolute time value. */
  lastEndedAt: string | null;
}

/** One node's state changed. */
export interface RuntimeDeltaMessage {
  type: "delta";
  nodeId: string;
  status: NodeStatus;
  runCount: number;
  errorCount: number;
  lastError: RuntimeErrorInfo | null;
  lastEndedAt: string | null;
}

/** Sent once, right after connect: every currently-tracked node's state. */
export interface RuntimeSnapshotMessage {
  type: "snapshot";
  states: NodeStateEntry[];
}

/** One running (or failed-to-start) trace source's status (Task 22).
 *  `"starting"` is transient (between `create_source` and the first
 *  status read); a healthy file-tail source reports `"waiting"` (the
 *  target hasn't created its log file yet) or `"running"`; `"error"`
 *  covers both "failed to start" and "crashed mid-run," with `detail`
 *  carrying the message. `eventsReceived`/`eventsIgnored` are scoped to
 *  THIS source -- unlike `DeadLetterStatsMessage.count`, the dead-letter
 *  buffer's cross-source total. */
export interface SourceStatus {
  methodId: string;
  label: string;
  state: "starting" | "waiting" | "running" | "error";
  detail: string | null;
  eventsReceived: number;
  eventsIgnored: number;
}

/** Sent periodically (every 2s). `count` is the dead-letter buffer's own
 *  size; `droppedCount`/`parseErrorCount`/`nonTraceCount` come from every
 *  currently-running file-tailing source's stats, summed -- a different
 *  layer's concern (log parsing vs. resolution) than `count`.
 *
 *  `sources` (Task 22) replaces the single-source `sourcePath`/
 *  `sourceState`/`eventsReceived` fields Task 21 added: the backend can
 *  run several trace sources at once now (the Setup screen's "Runtime
 *  tracing" section), so "where is THE source" no longer makes sense --
 *  see `components/RuntimeStatusChip.tsx` for how the frontend
 *  summarizes a list instead of one path/state pair. `eventsResolved`
 *  stays a single TOTAL across every source (per-source detail lives in
 *  each `SourceStatus.eventsReceived`/`eventsIgnored` instead). */
export interface DeadLetterStatsMessage {
  type: "dead_letter_stats";
  count: number;
  droppedCount: number;
  parseErrorCount: number;
  nonTraceCount: number;
  eventsResolved: number;
  sources: SourceStatus[];
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

// --- Trace methods (Task 22) -----------------------------------------------
// The Setup screen's "Runtime tracing" section is generated entirely from
// these -- no method-specific frontend code. Mirrors
// `runtime/methods/base.py`'s `TraceMethodInfo` and `main.py`'s
// `MethodConfigOut`/`RuntimeConfigOut`.

/** One registered trace method, as `GET /runtime/methods` describes it. */
export interface TraceMethodInfo {
  id: string;
  label: string;
  description: string;
  /** Shown in an amber box under this method's row whenever not null --
   *  regardless of whether the method is currently checked. */
  warning: string | null;
  /** True when this method can show a node as `active` WHILE a call is
   *  still running. False for after-the-fact methods (Task 24) that only
   *  ever report completed calls. */
  liveState: boolean;
  /** Copyable setup text. `{field}` placeholders are filled from the
   *  method's own settings field values (e.g. `{path}`). */
  setupHint: string | null;
  /** JSON Schema of the settings model -- flat `string`/`integer`/`boolean`
   *  properties only. `components/setup/SchemaFields.tsx` renders it. */
  settingsSchema: Record<string, unknown>;
}

/** One configured method's id + its current settings (echoed back by
 *  `GET`/`PUT /runtime/config`, and what the Setup screen sends on
 *  `PUT`). `settings`' shape is whatever that method's own
 *  `settingsSchema` describes -- opaque here, validated server-side. */
export interface MethodConfigEntry {
  id: string;
  settings: Record<string, unknown>;
}

/** `GET /runtime/config`'s shape, and `PUT /runtime/config`'s response
 *  (the config AFTER applying the request, not a plain echo of it). */
export interface RuntimeConfig {
  methods: MethodConfigEntry[];
  sources: SourceStatus[];
}
