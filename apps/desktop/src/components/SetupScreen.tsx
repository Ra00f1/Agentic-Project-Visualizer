/**
 * Setup screen — the first thing the user sees.
 *
 * Two jobs:
 *   1. Take the connection details (Mongo URI + DB name).
 *   2. Show every collection on that DB with its document count, so the user
 *      can uncheck heavy ones before we scan.
 *
 * Flow:
 *   - Component mounts with default URI/DB. Auto-fires `fetchSchema` after a
 *     short debounce whenever URI/DB change.
 *   - Response populates the collection list. All are checked by default.
 *   - Collections whose document_count exceeds `LARGE_THRESHOLD` show a
 *     warning icon with a tooltip so the user knows they're paying for it.
 *   - "Continue" calls the parent's onSubmit with the chosen selection.
 *
 * We deliberately don't persist the selection anywhere — it lives only in
 * the parent component's state for this session. If the user restarts the
 * app, they see the setup screen again. localStorage persistence is a
 * follow-up slice.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CollectionSchema } from "@shared-types/graph";
import { fetchSchema } from "../api";

/** Collections above this doc count get a warning icon. Chosen to be low
 *  enough that a real "thousands of files" collection stands out visually. */
const LARGE_THRESHOLD = 500;

/** Collections our L1 scanner currently knows how to interpret. Others are
 *  shown but disabled — the user can see they exist but not scan them. */
const KNOWN_COLLECTIONS: ReadonlySet<string> = new Set([
  "workflows",
  "agents",
  "models",
  "tools",
  "files",
  // "users" and "prompts" also known but excluded from the current mapping
  // (see l1_entity.py). Toggle them here when the mapping restores them.
]);

export interface SetupScreenProps {
  initialUri: string;
  initialDb: string;
  /** Prefilled codebase root — used to remember what the user last typed.
   *  Empty string means L2 will be silently skipped until they fill it in. */
  initialCodebaseRoot: string;
  onSubmit: (params: {
    uri: string;
    db: string;
    collections: string[];
    /** Trimmed. Empty string means "don't run L2." */
    codebaseRoot: string;
  }) => void;
}

export function SetupScreen({
  initialUri,
  initialDb,
  initialCodebaseRoot,
  onSubmit,
}: SetupScreenProps) {
  const [uri, setUri] = useState(initialUri);
  const [db, setDb] = useState(initialDb);
  const [codebaseRoot, setCodebaseRoot] = useState(initialCodebaseRoot);

  const [schema, setSchema] = useState<CollectionSchema[] | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // Debounce the auto-fetch so we don't hammer the backend as the user
  // types their URI character by character.
  const debounceRef = useRef<number | null>(null);
  const load = useCallback(async () => {
    setStatus("loading");
    setError(null);
    try {
      const list = await fetchSchema({ uri, db });
      setSchema(list);
      // Default selection: every KNOWN collection. Anything else is available
      // to inspect the count but not check.
      const defaults = new Set(list.filter((s) => KNOWN_COLLECTIONS.has(s.name)).map((s) => s.name));
      setSelected(defaults);
      setStatus("ready");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSchema(null);
      setStatus("error");
    }
  }, [uri, db]);

  useEffect(() => {
    if (debounceRef.current) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => void load(), 500);
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
    };
  }, [load]);

  const toggle = useCallback((name: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }, []);

  const totalSelected = selected.size;
  const canContinue = status === "ready" && totalSelected > 0;

  const knownSchema = useMemo(
    () => (schema ?? []).filter((s) => KNOWN_COLLECTIONS.has(s.name)),
    [schema],
  );
  const otherSchema = useMemo(
    () => (schema ?? []).filter((s) => !KNOWN_COLLECTIONS.has(s.name)),
    [schema],
  );

  return (
    <div className="flex h-screen w-screen items-start justify-center overflow-auto bg-neutral-50 px-4 py-10 dark:bg-neutral-950">
      <div className="w-full max-w-2xl">
        <h1 className="mb-1 text-2xl font-semibold text-neutral-900 dark:text-neutral-50">
          Agentic Project Visualizer
        </h1>
        <p className="mb-6 text-sm text-neutral-600 dark:text-neutral-400">
          Point the app at your database, then choose which collections to include in the graph.
        </p>

        <section className="mb-6 rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
          <h2 className="mb-3 text-sm font-semibold text-neutral-900 dark:text-neutral-100">
            Connection
          </h2>
          <div className="grid grid-cols-1 gap-3">
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-neutral-600 dark:text-neutral-400">MongoDB URI</span>
              <input
                className="rounded border border-neutral-300 bg-white px-2 py-1.5 font-mono text-xs text-neutral-900 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100"
                value={uri}
                onChange={(e) => setUri(e.target.value)}
                spellCheck={false}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-neutral-600 dark:text-neutral-400">Database name</span>
              <input
                className="rounded border border-neutral-300 bg-white px-2 py-1.5 font-mono text-xs text-neutral-900 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100"
                value={db}
                onChange={(e) => setDb(e.target.value)}
                spellCheck={false}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-neutral-600 dark:text-neutral-400">
                Codebase root{" "}
                <span className="text-xs text-neutral-400 dark:text-neutral-500">
                  (optional — enables L2: tool → code resolution)
                </span>
              </span>
              <input
                className="rounded border border-neutral-300 bg-white px-2 py-1.5 font-mono text-xs text-neutral-900 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100"
                value={codebaseRoot}
                onChange={(e) => setCodebaseRoot(e.target.value)}
                placeholder="C:\path\to\your\project"
                spellCheck={false}
              />
            </label>
          </div>
        </section>

        <section className="mb-6 rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-neutral-900 dark:text-neutral-100">
              Collections
            </h2>
            <span className="text-xs text-neutral-500 dark:text-neutral-400">
              {status === "loading" && "Loading…"}
              {status === "ready" && `${totalSelected} of ${knownSchema.length} selected`}
              {status === "error" && (
                <span className="text-red-600 dark:text-red-400">Error — see below</span>
              )}
            </span>
          </div>

          {status === "error" && (
            <div className="mb-3 rounded border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
              {error}
            </div>
          )}

          {status === "ready" && (
            <>
              <div className="mb-2 text-xs font-medium text-neutral-500 dark:text-neutral-400">
                Recognized by the scanner
              </div>
              <ul className="mb-4 divide-y divide-neutral-100 dark:divide-neutral-800">
                {knownSchema.map((c) => (
                  <CollectionRow
                    key={c.name}
                    coll={c}
                    checked={selected.has(c.name)}
                    disabled={false}
                    onToggle={() => toggle(c.name)}
                  />
                ))}
                {knownSchema.length === 0 && (
                  <li className="py-2 text-xs italic text-neutral-500 dark:text-neutral-400">
                    None of the recognized collections were found on this DB.
                  </li>
                )}
              </ul>

              {otherSchema.length > 0 && (
                <>
                  <div className="mb-2 text-xs font-medium text-neutral-500 dark:text-neutral-400">
                    Other collections (not scanned by v1)
                  </div>
                  <ul className="divide-y divide-neutral-100 dark:divide-neutral-800">
                    {otherSchema.map((c) => (
                      <CollectionRow key={c.name} coll={c} checked={false} disabled={true} onToggle={() => {}} />
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
        </section>

        <div className="flex items-center justify-end gap-3">
          <button
            onClick={() => void load()}
            disabled={status === "loading"}
            className="rounded border border-neutral-300 px-3 py-1.5 text-sm text-neutral-900 disabled:opacity-40 dark:border-neutral-700 dark:text-neutral-100"
          >
            Reload
          </button>
          <button
            onClick={() => onSubmit({ uri, db, collections: [...selected],
              codebaseRoot,
            })}
            disabled={!canContinue}
            className="rounded bg-neutral-900 px-4 py-1.5 text-sm text-white disabled:opacity-40 dark:bg-neutral-100 dark:text-neutral-900"
          >
            Continue →
          </button>
        </div>
      </div>
    </div>
  );
}

function CollectionRow({
  coll,
  checked,
  disabled,
  onToggle,
}: {
  coll: CollectionSchema;
  checked: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  const isLarge = (coll.documentCount ?? 0) > LARGE_THRESHOLD;
  return (
    <li className="flex items-center gap-3 py-2">
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        disabled={disabled}
        className="h-4 w-4 accent-neutral-900 dark:accent-neutral-100"
      />
      <span
        className={
          "flex-1 font-mono text-xs " +
          (disabled
            ? "text-neutral-400 dark:text-neutral-600"
            : "text-neutral-900 dark:text-neutral-100")
        }
      >
        {coll.name}
      </span>
      {isLarge && (
        <span
          title={`This is a large collection (${coll.documentCount!.toLocaleString()} docs). Consider unchecking if you don't need it for the graph.`}
          className="flex items-center gap-1 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-900 dark:bg-amber-950 dark:text-amber-300"
        >
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <path d="M12 9v4M12 17h.01M4.93 19h14.14a2 2 0 0 0 1.72-3L13.72 4.87a2 2 0 0 0-3.44 0L3.21 16a2 2 0 0 0 1.72 3z" />
          </svg>
          large
        </span>
      )}
      <span className="min-w-[70px] text-right font-mono text-[11px] tabular-nums text-neutral-500 dark:text-neutral-400">
        {coll.documentCount === null ? "—" : coll.documentCount.toLocaleString()}
      </span>
    </li>
  );
}
