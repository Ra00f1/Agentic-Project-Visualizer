/**
 * One row in `TracingMethods.tsx`'s list: checkbox, label, "Live" badge,
 * description, an always-visible warning box, and (once checked) the
 * generated settings fields plus the setup hint with a Copy button.
 * Split out of `TracingMethods.tsx` purely for file size (CLAUDE.md §5) --
 * `TracingMethods` owns all the state; this is pure rendering.
 */

import { useState } from "react";
import { SchemaFields } from "./SchemaFields";
import type { SourceStatus, TraceMethodInfo } from "@shared-types/runtime";

export interface MethodState {
  checked: boolean;
  settings: Record<string, unknown>;
}

export function MethodRow({
  method,
  state,
  sourceStatus,
  onToggle,
  onFieldChange,
}: {
  method: TraceMethodInfo;
  state: MethodState;
  sourceStatus: SourceStatus | null;
  onToggle: () => void;
  onFieldChange: (field: string, value: unknown) => void;
}) {
  const setupHint =
    state.checked && method.setupHint ? fillPlaceholders(method.setupHint, state.settings) : null;

  return (
    <div className="rounded border border-neutral-200 p-3 dark:border-neutral-800">
      <label className="flex items-start gap-2">
        <input
          type="checkbox"
          checked={state.checked}
          onChange={onToggle}
          aria-label={method.label}
          className="mt-0.5 h-4 w-4 accent-neutral-900 dark:accent-neutral-100"
        />
        <span className="flex-1">
          <span className="flex items-center gap-2">
            <span className="text-sm font-medium text-neutral-900 dark:text-neutral-100">{method.label}</span>
            {method.liveState && (
              <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300">
                Live
              </span>
            )}
          </span>
          <span className="block text-xs text-neutral-500 dark:text-neutral-400">{method.description}</span>
        </span>
      </label>

      {method.warning && (
        <div className="mt-2 flex items-start gap-2 rounded border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            className="mt-0.5 shrink-0"
          >
            <path d="M12 9v4M12 17h.01M4.93 19h14.14a2 2 0 0 0 1.72-3L13.72 4.87a2 2 0 0 0-3.44 0L3.21 16a2 2 0 0 0 1.72 3z" />
          </svg>
          <span>{method.warning}</span>
        </div>
      )}

      {state.checked && (
        <div className="mt-3 flex flex-col gap-3 border-t border-neutral-100 pt-3 dark:border-neutral-800">
          <SchemaFields schema={method.settingsSchema} values={state.settings} onChange={onFieldChange} />
          {setupHint && <SetupHint text={setupHint} />}
          {sourceStatus?.state === "error" && sourceStatus.detail && (
            <p className="text-xs text-red-600 dark:text-red-400">{sourceStatus.detail}</p>
          )}
        </div>
      )}
    </div>
  );
}

/** `{field}` placeholders filled from the method's current settings
 *  values. A placeholder with no matching value (or no value yet) is left
 *  as-is -- better than silently dropping the text the user still needs
 *  to fill in by hand. */
function fillPlaceholders(template: string, values: Record<string, unknown>): string {
  return template.replace(/\{(\w+)\}/g, (match, key: string) => {
    const value = values[key];
    return value === undefined || value === null ? match : String(value);
  });
}

function SetupHint({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative">
      <pre className="overflow-auto rounded border border-neutral-200 bg-neutral-50 p-2 font-mono text-[11px] leading-relaxed text-neutral-800 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-200">
        {text}
      </pre>
      <button
        type="button"
        onClick={() => {
          void navigator.clipboard.writeText(text).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          });
        }}
        className="absolute right-1.5 top-1.5 rounded border border-neutral-300 bg-white px-1.5 py-0.5 text-[10px] text-neutral-700 hover:bg-neutral-100 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-300 dark:hover:bg-neutral-700"
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
