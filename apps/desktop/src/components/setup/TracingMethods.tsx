/**
 * Setup screen section: lists every registered trace method (Task 22) and
 * lets the user turn any of them on with its own settings, generated
 * entirely from `GET /runtime/methods` -- no method-specific frontend code.
 *
 * Initial checked state + settings, in order:
 *   1. `apv.runtime.methods` in localStorage, if present.
 *   2. Otherwise `GET /runtime/config`, so the env-var startup default
 *      (`AGENTIC_DEFAULT_TRACE_LOG_PATH`) shows up as already checked.
 *   3. Otherwise everything unchecked.
 *
 * Owns its own state (which methods are checked, each one's settings) and
 * reports the current `[{id, settings}]` list up via `onChange` on every
 * change -- the PARENT (`SetupScreen`) saves to localStorage and PUTs it
 * only when Continue is actually clicked, not on every keystroke (Task
 * 22's own Design: "On Continue... 1. Save... 2. PUT...").
 *
 * `putErrorMessage`/`sourceStatuses` are set by the parent AFTER a
 * Continue attempt: a 422 shows as a banner at the top of this section
 * (today's one settings field, `apv_live_file.path`, makes a literal
 * per-field placement mostly cosmetic -- flagged as a deviation in this
 * task's own Result), and a source that came back `state: "error"` (e.g.
 * a bad path) shows its `detail` under that method's own row.
 */

import { useEffect, useRef, useState } from "react";
import { fetchRuntimeConfig, fetchTraceMethods } from "../../api";
import { MethodRow, type MethodState } from "./TracingMethodRow";
import type { MethodConfigEntry, SourceStatus, TraceMethodInfo } from "@shared-types/runtime";

/** Exported for `BackendGate.tsx`, which re-applies this same saved
 *  config on every app start (a fresh backend process has no memory of
 *  it) -- see that file's own comment for why it reads this directly
 *  rather than going through this component. */
export const RUNTIME_METHODS_STORAGE_KEY = "apv.runtime.methods";

export interface TracingMethodsProps {
  onChange: (entries: MethodConfigEntry[]) => void;
  putErrorMessage?: string | null;
  sourceStatuses?: SourceStatus[] | null;
}

export function loadSavedTracingEntries(): MethodConfigEntry[] | null {
  try {
    const raw = localStorage.getItem(RUNTIME_METHODS_STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as MethodConfigEntry[]) : null;
  } catch {
    return null; // broken JSON / blocked storage -- fall through to the next source
  }
}

/** Pulls each property's `default` out of a method's settings JSON Schema.
 *  `SchemaFields` already falls back to these for DISPLAY when a field
 *  isn't in `values` yet, but that fallback is local to that render --
 *  without also seeding them into `state.settings` here, the setup
 *  hint's `{field}` placeholders (which read `state.settings` directly)
 *  would show unfilled even though the input right above them already
 *  displays the default value. */
function defaultsFromSchema(schema: Record<string, unknown>): Record<string, unknown> {
  const rawProperties = schema.properties;
  if (!rawProperties || typeof rawProperties !== "object") return {};
  const defaults: Record<string, unknown> = {};
  for (const [name, fieldSchema] of Object.entries(rawProperties as Record<string, unknown>)) {
    if (fieldSchema && typeof fieldSchema === "object" && "default" in fieldSchema) {
      defaults[name] = (fieldSchema as { default: unknown }).default;
    }
  }
  return defaults;
}

export function TracingMethods({ onChange, putErrorMessage, sourceStatuses }: TracingMethodsProps) {
  const [methods, setMethods] = useState<TraceMethodInfo[] | null>(null);
  const [rows, setRows] = useState<Record<string, MethodState>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  // Guards the onChange effect below against firing with an empty `rows`
  // before initial-value resolution (localStorage -> GET /runtime/config)
  // has actually finished -- otherwise the parent would briefly see
  // "nothing checked" even when the env-var default already turned a
  // method on.
  const resolvedRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const methodList = await fetchTraceMethods();
        if (cancelled) return;
        // Deliberately NOT `setMethods(methodList)` here, before initial
        // resolution: that would render interactive, all-unchecked
        // checkboxes during the gap while GET /runtime/config is still in
        // flight below -- a click in that window would just get silently
        // overwritten the moment resolution finishes and calls `setRows`.
        // `methods` stays null (rendering "Loading...") until BOTH calls
        // below have resolved, so rows and their checkboxes never exist
        // before they're ready to be clicked.

        let initial = loadSavedTracingEntries();
        if (initial === null) {
          try {
            initial = (await fetchRuntimeConfig()).methods;
          } catch {
            initial = []; // backend unreachable for this call -- "everything unchecked"
          }
          if (cancelled) return;
        }

        const nextRows: Record<string, MethodState> = {};
        for (const method of methodList) {
          const match = initial.find((e) => e.id === method.id);
          nextRows[method.id] = {
            checked: match !== undefined,
            settings: { ...defaultsFromSchema(method.settingsSchema), ...match?.settings },
          };
        }
        resolvedRef.current = true;
        setRows(nextRows);
        setMethods(methodList); // last: this is what makes the rows (and their checkboxes) appear at all
      } catch (err) {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!resolvedRef.current) return;
    const entries: MethodConfigEntry[] = Object.entries(rows)
      .filter(([, state]) => state.checked)
      .map(([id, state]) => ({ id, settings: state.settings }));
    onChange(entries);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows]);

  if (loadError) {
    return (
      <p className="text-xs text-red-600 dark:text-red-400">
        Couldn't load runtime tracing methods: {loadError}
      </p>
    );
  }
  if (methods === null) {
    return <p className="text-xs text-neutral-500 dark:text-neutral-400">Loading…</p>;
  }

  return (
    <div className="flex flex-col gap-3">
      {putErrorMessage && (
        <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
          {putErrorMessage}
        </div>
      )}
      {methods.map((method) => (
        <MethodRow
          key={method.id}
          method={method}
          state={rows[method.id] ?? { checked: false, settings: {} }}
          sourceStatus={sourceStatuses?.find((s) => s.methodId === method.id) ?? null}
          onToggle={() =>
            setRows((prev) => ({
              ...prev,
              [method.id]: {
                checked: !(prev[method.id]?.checked ?? false),
                settings: prev[method.id]?.settings ?? defaultsFromSchema(method.settingsSchema),
              },
            }))
          }
          onFieldChange={(field, value) =>
            setRows((prev) => ({
              ...prev,
              [method.id]: {
                checked: prev[method.id]?.checked ?? false,
                settings: { ...prev[method.id]?.settings, [field]: value },
              },
            }))
          }
        />
      ))}
      {methods.length === 0 && (
        <p className="text-xs italic text-neutral-500 dark:text-neutral-400">
          No runtime tracing methods are registered.
        </p>
      )}
    </div>
  );
}
