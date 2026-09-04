/**
 * Right-side detail panel.
 *
 * Displays everything the backend knows about a node: its identity, its
 * provenance (which scanner produced it and where the data came from), and
 * its full raw attributes payload from the source (Mongo document, in this
 * slice). Per CLAUDE.md §7 the detail panel is a first-class concept —
 * this is v1 of it.
 *
 * Rendering strategy for attributes:
 *   - Scalars (string, number, boolean, null) → shown as a definition list
 *     row with a monospace value.
 *   - Arrays and objects → collapsed as JSON in a bordered `<pre>` block so
 *     the panel doesn't blow up vertically on deeply nested payloads.
 *
 * The panel is intentionally NOT a modal — it lives alongside the graph so
 * the user can click other nodes to compare. Closing is via the X button or
 * pressing Escape when the panel has focus.
 */

import { useEffect } from "react";
import type { GraphNode } from "@shared-types/graph";
import { NODE_STYLE } from "../lib/nodeStyle";

export interface DetailPanelProps {
  /** The raw domain node (post-clone-resolution — always the underlying entity). */
  node: GraphNode;
  /**
   * If the user right-clicked a clone (`<originalId>@<consumerId>`), pass the
   * consumer's display name here so the panel can hint that context. Optional.
   */
  clonedFromContext?: string | null;
  onClose: () => void;
}

/** Type guard: is this value a "plain object" (not array, not null)? */
function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Render one attribute row. Scalars inline, complex values as JSON block. */
function AttributeRow({ label, value }: { label: string; value: unknown }) {
  const isComplex = Array.isArray(value) || isPlainObject(value);
  return (
    <div className="border-b border-neutral-100 py-2 last:border-b-0 dark:border-neutral-800">
      <div className="mb-1 text-xs font-medium text-neutral-500 dark:text-neutral-400">{label}</div>
      {isComplex ? (
        <pre className="max-h-64 overflow-auto rounded border border-neutral-200 bg-neutral-50 p-2 font-mono text-[11px] leading-relaxed text-neutral-800 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-200">
          {JSON.stringify(value, null, 2)}
        </pre>
      ) : (
        <div className="break-all font-mono text-xs text-neutral-800 dark:text-neutral-200">
          {value === null || value === undefined ? (
            <span className="italic text-neutral-400 dark:text-neutral-500">null</span>
          ) : (
            String(value)
          )}
        </div>
      )}
    </div>
  );
}

export function DetailPanel({ node, clonedFromContext, onClose }: DetailPanelProps) {
  const style = NODE_STYLE[node.type];

  // Escape closes the panel — mirror the shortcut we already use for the
  // context menu so the whole "escape closes the last thing I opened"
  // pattern is consistent.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Attribute keys sorted with a couple of common fields lifted to the top
  // so scanning the panel feels predictable across node types. Everything
  // else keeps the source order.
  const PRIORITY_KEYS = ["description", "display_name", "framework", "owner_id", "created_by"];
  const attributeKeys = Object.keys(node.attributes ?? {});
  const orderedKeys = [
    ...PRIORITY_KEYS.filter((k) => attributeKeys.includes(k)),
    ...attributeKeys.filter((k) => !PRIORITY_KEYS.includes(k)),
  ];

  return (
    <aside
      className="flex h-full w-[380px] shrink-0 flex-col border-l border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-950"
      aria-label={`Details for ${node.name}`}
    >
      <header className="flex items-start gap-2 border-b border-neutral-200 px-4 py-3 dark:border-neutral-800">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span
              className="inline-block h-2.5 w-2.5 rounded-sm"
              style={{ background: style.fill, border: `1.5px solid ${style.border}` }}
              aria-hidden
            />
            <span className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
              {node.type}
            </span>
          </div>
          <h2
            className="mt-1 truncate text-base font-semibold text-neutral-900 dark:text-neutral-50"
            title={node.name}
          >
            {node.name}
          </h2>
          {clonedFromContext && (
            <p className="mt-0.5 text-xs italic text-neutral-500 dark:text-neutral-400">
              viewed via <span className="font-medium">{clonedFromContext}</span>
            </p>
          )}
        </div>
        <button
          onClick={onClose}
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
        <section className="mb-4">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
            Identity
          </h3>
          <div className="rounded border border-neutral-200 bg-neutral-50 px-2 py-1.5 dark:border-neutral-800 dark:bg-neutral-900">
            <div className="mb-0.5 text-[10px] uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
              Node ID
            </div>
            <div className="break-all font-mono text-[11px] text-neutral-800 dark:text-neutral-200">
              {node.id}
            </div>
          </div>
        </section>

        <section className="mb-4">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
            Provenance
          </h3>
          <AttributeRow label="scanner" value={node.provenance.source} />
          <AttributeRow label="source ref" value={node.provenance.sourceRef} />
          <AttributeRow label="scanned at" value={node.provenance.scannedAt} />
        </section>

        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
            Attributes ({orderedKeys.length})
          </h3>
          {orderedKeys.length === 0 ? (
            <p className="italic text-xs text-neutral-500 dark:text-neutral-400">
              No attributes on this node.
            </p>
          ) : (
            orderedKeys.map((k) => (
              <AttributeRow key={k} label={k} value={node.attributes[k]} />
            ))
          )}
        </section>
      </div>
    </aside>
  );
}
