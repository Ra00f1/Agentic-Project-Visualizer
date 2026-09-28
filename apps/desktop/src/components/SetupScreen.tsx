/**
 * Setup screen — the first thing the user sees.
 *
 * Three jobs:
 *   1. Take the connection details (Mongo URI + DB name + codebase root).
 *   2. List every collection on that DB with its document count.
 *   3. Map each collection to a **node role + field names** — the four
 *      pieces of information the L1 scanner needs to build the graph
 *      from a project whose schema doesn't happen to match our mock
 *      fixture's field names verbatim:
 *
 *        - role            — workflow / agent / tool / model / prompt /
 *                            user / file / other (drops it from the graph)
 *        - id_field        — the doc field holding the primary key.
 *                            "_id" for Mongo; a real project might use
 *                            "agent_uuid" or "pk".
 *        - name_field      — the human-readable name for the node label.
 *        - parent_ref_fields — comma-separated list of doc fields that
 *                            hold foreign-key refs to other collections.
 *                            The backend infers the target collection
 *                            from each field name via the standard
 *                            `<X>_id → role X` convention.
 *
 * Auto-pick strategy:
 *   Name heuristic for role (workflows → workflow, agents → agent, etc.).
 *   Field-name heuristic for parent refs: everything in the sampled
 *   schema that ends in `_id` / `_ids` EXCLUDING the primary key and
 *   the audit-trail fields (`created_by`, `updated_by`, `owner_id`) —
 *   those aren't architectural relationships, and turning them into
 *   edges would make user nodes into massive central hubs.
 *   Anything a user has explicitly picked wins over the heuristic on
 *   next open (persisted in localStorage keyed on `<uri>.<db>`).
 *
 * Missing-vital warning:
 *   Unchanged from the previous iteration — if the selected set has no
 *   collection mapped to workflow / agent / tool, the amber strip
 *   surfaces above the Continue button.
 *
 * Backward-compat note:
 *   Older sessions persisted only the role under `apv.roles.<uri>.<db>`.
 *   On first load we try `apv.mapping.<uri>.<db>` first, then fall back
 *   to the old key for the role and auto-fill everything else. Users
 *   don't lose their role choices in the upgrade.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CollectionMappingEntry, CollectionSchema } from "@shared-types/graph";
import { fetchSchema } from "../api";

/** Every role a collection can play in the visualizer. Mirror of
 *  CollectionRole in @shared-types/graph. */
export type Role = CollectionMappingEntry["role"];

const ROLES: Role[] = ["workflow", "agent", "tool", "model", "prompt", "user", "file", "other"];

/** Role display + color swatch. Values mirror the node palette in
 *  nodeStyle.ts / index.css so the setup screen reads as the same
 *  visual language as the graph. */
const ROLE_META: Record<Role, { label: string; color: string }> = {
  workflow: { label: "workflow", color: "#D97706" },
  agent:    { label: "agent",    color: "#2563EB" },
  tool:     { label: "tool",     color: "#059669" },
  model:    { label: "model",    color: "#7C3AED" },
  prompt:   { label: "prompt",   color: "#DB2777" },
  user:     { label: "user",     color: "#4B5563" },
  file:     { label: "file",     color: "#0284C7" },
  other:    { label: "other",    color: "#a3a3a3" },
};

/** Roles a project MUST have at least one collection mapped to, or the
 *  graph won't show anything meaningful. */
const VITAL_ROLES: Role[] = ["workflow", "agent", "tool"];

/** Collections above this doc count get a "large" warning icon. */
const LARGE_THRESHOLD = 500;

/** Fields that look like refs by their name suffix but are actually
 *  audit-trail bookkeeping — creating edges from them would turn every
 *  user into a central hub. Kept as a set so the auto-picker can filter
 *  cheaply. */
const NON_REF_FIELDS: ReadonlySet<string> = new Set([
  "_id",
  "created_by",
  "updated_by",
  "owner_id",
  "created_at",
  "updated_at",
]);

/** Auto-pick a role from a collection name. Same heuristic the L1
 *  scanner has used since v0 — kept in sync manually. */
function autoPickRole(name: string): Role {
  const n = name.trim().toLowerCase();
  if (n === "workflows" || n === "workflow") return "workflow";
  if (n === "agents" || n === "agent") return "agent";
  if (n === "tools" || n === "tool") return "tool";
  if (n === "models" || n === "model") return "model";
  if (n === "prompts" || n === "prompt") return "prompt";
  if (n === "users" || n === "user") return "user";
  if (n === "files" || n === "file" || n === "documents") return "file";
  return "other";
}

/** Auto-pick the display-name field for a collection. Prefers
 *  `display_name` when the schema sample contains it (matches the
 *  DEFAULT_COLLECTION_MAPPING behavior for agents and workflows);
 *  falls back to `name`. */
function autoPickNameField(role: Role, fieldNames: readonly string[]): string {
  const fields = new Set(fieldNames);
  if ((role === "agent" || role === "workflow") && fields.has("display_name")) {
    return "display_name";
  }
  if (fields.has("name")) return "name";
  if (fields.has("display_name")) return "display_name";
  if (fields.has("title")) return "title";
  return "name"; // safe default even when missing — L1 falls back to _id
}

/** Auto-pick reference fields: everything ending in _id / _ids that
 *  isn't a primary key or an audit-trail field. */
function autoPickRefFields(fieldNames: readonly string[]): string[] {
  return fieldNames.filter(
    (f) => (f.endsWith("_id") || f.endsWith("_ids")) && !NON_REF_FIELDS.has(f),
  );
}

/** localStorage key for the per-(uri, db) mapping. Suffix bumped from
 *  `roles` to `mapping` when this UI added id_field / name_field /
 *  parent_ref_fields. Old entries are read once for backward compat. */
function mappingKey(uri: string, db: string): string {
  return `apv.mapping.${uri}.${db}`;
}
function legacyRolesKey(uri: string, db: string): string {
  return `apv.roles.${uri}.${db}`;
}

export interface SetupScreenProps {
  initialUri: string;
  initialDb: string;
  initialCodebaseRoot: string;
  onSubmit: (params: {
    uri: string;
    db: string;
    collections: string[];
    codebaseRoot: string;
    /** Full mapping — role + id_field + name_field + parent_ref_fields per
     *  collection. Includes rows the user unchecked so the parent can
     *  persist their state. Rows with role === "other" are still included
     *  here; the fetchGraph call drops them before sending to the backend. */
    collectionMapping: Record<string, CollectionMappingEntry>;
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
  const [mappings, setMappings] = useState<Record<string, CollectionMappingEntry>>({});

  const debounceRef = useRef<number | null>(null);
  const load = useCallback(async () => {
    setStatus("loading");
    setError(null);
    try {
      const list = await fetchSchema({ uri, db });
      setSchema(list);

      // Restore any previously-saved mapping for this (uri, db).
      let persistedMapping: Record<string, CollectionMappingEntry> = {};
      let persistedLegacyRoles: Record<string, Role> = {};
      try {
        const rawMapping = localStorage.getItem(mappingKey(uri, db));
        if (rawMapping) {
          persistedMapping = JSON.parse(rawMapping) as Record<string, CollectionMappingEntry>;
        }
        // Legacy fallback: pre-mapping sessions only stored the role.
        const rawLegacy = localStorage.getItem(legacyRolesKey(uri, db));
        if (rawLegacy) {
          persistedLegacyRoles = JSON.parse(rawLegacy) as Record<string, Role>;
        }
      } catch {
        // Broken JSON / blocked storage — fall through to pure auto-pick.
      }

      const nextMapping: Record<string, CollectionMappingEntry> = {};
      for (const c of list) {
        const persisted = persistedMapping[c.name];
        if (persisted && (ROLES as readonly string[]).includes(persisted.role)) {
          // Trust the persisted entry, but backfill any missing keys
          // (older schemas may not have had all four fields).
          nextMapping[c.name] = {
            role: persisted.role,
            idField: persisted.idField ?? "_id",
            nameField:
              persisted.nameField ?? autoPickNameField(persisted.role, c.fieldNames),
            parentRefFields:
              persisted.parentRefFields ?? autoPickRefFields(c.fieldNames),
          };
          continue;
        }
        const legacyRole = persistedLegacyRoles[c.name];
        const role: Role =
          legacyRole && (ROLES as readonly string[]).includes(legacyRole)
            ? legacyRole
            : autoPickRole(c.name);
        nextMapping[c.name] = {
          role,
          idField: "_id",
          nameField: autoPickNameField(role, c.fieldNames),
          parentRefFields: autoPickRefFields(c.fieldNames),
        };
      }
      setMappings(nextMapping);
      // Default selection: every collection whose auto-picked role is
      // NOT "other". A user-marked "other" ends up excluded too.
      const defaults = new Set(
        list.map((c) => c.name).filter((n) => nextMapping[n]?.role !== "other"),
      );
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

  const changeRole = useCallback((name: string, role: Role) => {
    setMappings((prev) => {
      const cur = prev[name];
      if (!cur) return prev;
      return { ...prev, [name]: { ...cur, role } };
    });
    if (role !== "other") {
      setSelected((prev) => {
        if (prev.has(name)) return prev;
        const next = new Set(prev);
        next.add(name);
        return next;
      });
    }
  }, []);

  const changeField = useCallback(
    (name: string, key: "idField" | "nameField", value: string) => {
      setMappings((prev) => {
        const cur = prev[name];
        if (!cur) return prev;
        return { ...prev, [name]: { ...cur, [key]: value } };
      });
    },
    [],
  );

  const changeRefFields = useCallback((name: string, csv: string) => {
    // Split on commas, trim, drop empties. Empty CSV → empty list; the
    // user removed all refs deliberately (some collections legitimately
    // have none — e.g. models).
    const fields = csv
      .split(",")
      .map((f) => f.trim())
      .filter((f) => f.length > 0);
    setMappings((prev) => {
      const cur = prev[name];
      if (!cur) return prev;
      return { ...prev, [name]: { ...cur, parentRefFields: fields } };
    });
  }, []);

  const totalSelected = selected.size;

  const missingVital: Role[] = useMemo(() => {
    const mapped = new Set<Role>();
    for (const name of selected) {
      const r = mappings[name]?.role;
      if (r) mapped.add(r);
    }
    return VITAL_ROLES.filter((r) => !mapped.has(r));
  }, [selected, mappings]);

  const canContinue = status === "ready" && totalSelected > 0;

  return (
    <div className="flex h-screen w-screen items-start justify-center overflow-auto bg-neutral-50 px-4 py-10 dark:bg-neutral-950">
      <div className="w-full max-w-5xl">
        <h1 className="mb-1 text-2xl font-semibold text-neutral-900 dark:text-neutral-50">
          Agentic Project Visualizer
        </h1>
        <p className="mb-6 text-sm text-neutral-600 dark:text-neutral-400">
          Point the app at your database, tell it which collections describe your agent system,
          then continue to the graph.
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
            <div>
              <h2 className="text-sm font-semibold text-neutral-900 dark:text-neutral-100">
                Collections &amp; mapping
              </h2>
              <p className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">
                Every collection is mapped to a role plus the field names the scanner needs.
                Overrides are remembered per (URI, database).
              </p>
            </div>
            <span className="text-xs text-neutral-500 dark:text-neutral-400">
              {status === "loading" && "Loading…"}
              {status === "ready" && `${totalSelected} of ${schema?.length ?? 0} selected`}
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

          {status === "ready" && schema && (
            <>
              <div className="grid grid-cols-[24px_170px_130px_110px_120px_1fr_70px] items-center gap-2 border-b border-neutral-200 pb-1.5 text-[10px] font-semibold uppercase tracking-wider text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
                <span aria-hidden></span>
                <span>Collection</span>
                <span>Role</span>
                <span>ID field</span>
                <span>Name field</span>
                <span>Parent refs (comma-sep)</span>
                <span className="text-right">Docs</span>
              </div>
              <ul className="divide-y divide-neutral-100 dark:divide-neutral-800">
                {schema.map((c) => {
                  const entry = mappings[c.name];
                  if (!entry) return null;
                  return (
                    <CollectionRow
                      key={c.name}
                      coll={c}
                      checked={selected.has(c.name)}
                      entry={entry}
                      onToggle={() => toggle(c.name)}
                      onRoleChange={(r) => changeRole(c.name, r)}
                      onFieldChange={(k, v) => changeField(c.name, k, v)}
                      onRefFieldsChange={(csv) => changeRefFields(c.name, csv)}
                    />
                  );
                })}
                {schema.length === 0 && (
                  <li className="py-2 text-xs italic text-neutral-500 dark:text-neutral-400">
                    No collections found on this database.
                  </li>
                )}
              </ul>
            </>
          )}
        </section>

        {status === "ready" && missingVital.length > 0 && (
          <div className="mb-4 flex items-start gap-3 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              className="mt-0.5 shrink-0"
            >
              <path d="M12 9v4M12 17h.01M4.93 19h14.14a2 2 0 0 0 1.72-3L13.72 4.87a2 2 0 0 0-3.44 0L3.21 16a2 2 0 0 0 1.72 3z" />
            </svg>
            <div className="flex-1">
              <div className="font-medium">
                Missing role{missingVital.length === 1 ? "" : "s"}:{" "}
                {missingVital.map((r, i) => (
                  <span key={r}>
                    <span
                      className="inline-flex items-center gap-1 rounded-sm px-1.5 py-px font-mono text-xs"
                      style={{
                        background: ROLE_META[r].color + "26",
                        color: ROLE_META[r].color,
                      }}
                    >
                      {ROLE_META[r].label}
                    </span>
                    {i < missingVital.length - 1 ? ", " : ""}
                  </span>
                ))}
              </div>
              <p className="mt-1 text-xs text-amber-800 dark:text-amber-300/90">
                No selected collection is assigned this role. The graph will render but the pipeline
                will be missing this layer — pick a collection above and change its role, or
                continue anyway if this project genuinely doesn't have one.
              </p>
            </div>
          </div>
        )}

        <div className="flex items-center justify-end gap-3">
          <button
            onClick={() => void load()}
            disabled={status === "loading"}
            className="rounded border border-neutral-300 px-3 py-1.5 text-sm text-neutral-900 disabled:opacity-40 dark:border-neutral-700 dark:text-neutral-100"
          >
            Reload
          </button>
          <button
            onClick={() =>
              onSubmit({
                uri,
                db,
                collections: [...selected],
                codebaseRoot,
                collectionMapping: mappings,
              })
            }
            disabled={!canContinue}
            title={
              !canContinue
                ? "Select at least one collection before continuing."
                : missingVital.length > 0
                  ? `Warning: no collection is mapped to ${missingVital.join(", ")}.`
                  : undefined
            }
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
  entry,
  onToggle,
  onRoleChange,
  onFieldChange,
  onRefFieldsChange,
}: {
  coll: CollectionSchema;
  checked: boolean;
  entry: CollectionMappingEntry;
  onToggle: () => void;
  onRoleChange: (r: Role) => void;
  onFieldChange: (key: "idField" | "nameField", value: string) => void;
  onRefFieldsChange: (csv: string) => void;
}) {
  const isLarge = (coll.documentCount ?? 0) > LARGE_THRESHOLD;
  const refCsv = entry.parentRefFields.join(", ");
  return (
    <li className="grid grid-cols-[24px_170px_130px_110px_120px_1fr_70px] items-center gap-2 py-2">
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        aria-label={`Include ${coll.name} in the graph`}
        className="h-4 w-4 accent-neutral-900 dark:accent-neutral-100"
      />
      <div className="min-w-0">
        <div
          className={
            "truncate font-mono text-xs " +
            (checked
              ? "text-neutral-900 dark:text-neutral-100"
              : "text-neutral-400 dark:text-neutral-600")
          }
          title={coll.name}
        >
          {coll.name}
        </div>
        {isLarge && (
          <span
            title={`Large collection (${coll.documentCount!.toLocaleString()} docs). Consider unchecking if you don't need it.`}
            className="mt-0.5 inline-flex items-center gap-1 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-900 dark:bg-amber-950 dark:text-amber-300"
          >
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="M12 9v4M12 17h.01M4.93 19h14.14a2 2 0 0 0 1.72-3L13.72 4.87a2 2 0 0 0-3.44 0L3.21 16a2 2 0 0 0 1.72 3z" />
            </svg>
            large
          </span>
        )}
      </div>
      <div className="flex items-center gap-2">
        <span
          aria-hidden
          className="inline-block h-2.5 w-2.5 rounded-sm border"
          style={{
            background: ROLE_META[entry.role].color + "33",
            borderColor: ROLE_META[entry.role].color,
          }}
        />
        <select
          value={entry.role}
          onChange={(e) => onRoleChange(e.target.value as Role)}
          aria-label={`Role for ${coll.name}`}
          className="w-full rounded border border-neutral-300 bg-white px-2 py-1 text-xs font-medium text-neutral-900 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100"
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {ROLE_META[r].label}
            </option>
          ))}
        </select>
      </div>
      <input
        type="text"
        value={entry.idField}
        onChange={(e) => onFieldChange("idField", e.target.value)}
        aria-label={`ID field for ${coll.name}`}
        placeholder="_id"
        className="w-full rounded border border-neutral-300 bg-white px-2 py-1 font-mono text-[11px] text-neutral-900 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100"
      />
      <input
        type="text"
        value={entry.nameField}
        onChange={(e) => onFieldChange("nameField", e.target.value)}
        aria-label={`Name field for ${coll.name}`}
        placeholder="name"
        className="w-full rounded border border-neutral-300 bg-white px-2 py-1 font-mono text-[11px] text-neutral-900 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100"
      />
      <input
        type="text"
        value={refCsv}
        onChange={(e) => onRefFieldsChange(e.target.value)}
        aria-label={`Parent reference fields for ${coll.name}`}
        placeholder="agent_ids, model_id, tool_ids"
        className="w-full rounded border border-neutral-300 bg-white px-2 py-1 font-mono text-[11px] text-neutral-900 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100"
      />
      <span className="text-right font-mono text-[11px] tabular-nums text-neutral-500 dark:text-neutral-400">
        {coll.documentCount === null ? "—" : coll.documentCount.toLocaleString()}
      </span>
    </li>
  );
}
