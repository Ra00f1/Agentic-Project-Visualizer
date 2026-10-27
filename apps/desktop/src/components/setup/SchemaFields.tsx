/**
 * Renders a flat JSON Schema (as `TraceMethodInfo.settingsSchema` carries
 * it — `model_json_schema()`'s output of a Pydantic settings model) into
 * form inputs: `string` → text input, `integer` → number input, `boolean`
 * → checkbox. Labels come from `title` (falling back to the field name),
 * help text from `description`, initial values from `default` when the
 * caller hasn't supplied one yet via `values`.
 *
 * Anything outside that subset (object, array, a missing/malformed
 * `properties` entry) renders a one-line "unsupported setting type"
 * notice instead of crashing — Task 22's own Design: "It never crashes."
 * A future method needing a richer settings UI gets an `ui_component`
 * escape hatch at that point (not built now, per the task's own note).
 */

interface JsonSchemaProperty {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
}

function isJsonSchemaProperty(value: unknown): value is JsonSchemaProperty {
  return typeof value === "object" && value !== null;
}

export interface SchemaFieldsProps {
  /** `TraceMethodInfo.settingsSchema`, verbatim. */
  schema: Record<string, unknown>;
  /** Current field values, keyed by property name. A field not present
   *  here falls back to its schema `default`. */
  values: Record<string, unknown>;
  onChange: (name: string, value: unknown) => void;
}

export function SchemaFields({ schema, values, onChange }: SchemaFieldsProps) {
  const rawProperties = schema.properties;
  const properties: Record<string, unknown> =
    rawProperties && typeof rawProperties === "object" ? (rawProperties as Record<string, unknown>) : {};
  const fieldNames = Object.keys(properties);

  if (fieldNames.length === 0) return null; // nothing to configure for this method

  return (
    <div className="flex flex-col gap-2">
      {fieldNames.map((name) => {
        const fieldSchema = properties[name];
        if (!isJsonSchemaProperty(fieldSchema)) {
          return (
            <p key={name} className="text-xs italic text-neutral-500 dark:text-neutral-400">
              {name}: unsupported setting type
            </p>
          );
        }
        return (
          <SchemaField
            key={name}
            name={name}
            schema={fieldSchema}
            value={name in values ? values[name] : fieldSchema.default}
            onChange={(value) => onChange(name, value)}
          />
        );
      })}
    </div>
  );
}

function SchemaField({
  name,
  schema,
  value,
  onChange,
}: {
  name: string;
  schema: JsonSchemaProperty;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const label = schema.title ?? name;
  const id = `schema-field-${name}`;
  const inputClass =
    "rounded border border-neutral-300 bg-white px-2 py-1.5 font-mono text-xs text-neutral-900 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100";
  const help = schema.description && (
    <span className="text-xs text-neutral-400 dark:text-neutral-500">{schema.description}</span>
  );

  if (schema.type === "string") {
    return (
      <label htmlFor={id} className="flex flex-col gap-1 text-sm">
        <span className="text-neutral-600 dark:text-neutral-400">{label}</span>
        <input
          id={id}
          type="text"
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onChange(e.target.value)}
          spellCheck={false}
          className={inputClass}
        />
        {help}
      </label>
    );
  }

  if (schema.type === "integer") {
    return (
      <label htmlFor={id} className="flex flex-col gap-1 text-sm">
        <span className="text-neutral-600 dark:text-neutral-400">{label}</span>
        <input
          id={id}
          type="number"
          step={1}
          value={typeof value === "number" ? value : ""}
          onChange={(e) => {
            const parsed = e.target.value === "" ? undefined : Number(e.target.value);
            onChange(Number.isFinite(parsed) ? parsed : undefined);
          }}
          className={inputClass}
        />
        {help}
      </label>
    );
  }

  if (schema.type === "boolean") {
    return (
      <label htmlFor={id} className="flex items-center gap-2 text-sm">
        <input
          id={id}
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
          className="h-4 w-4 accent-neutral-900 dark:accent-neutral-100"
        />
        <span className="text-neutral-600 dark:text-neutral-400">{label}</span>
        {help}
      </label>
    );
  }

  return (
    <p className="text-xs italic text-neutral-500 dark:text-neutral-400">
      {label}: unsupported setting type{schema.type ? ` (${schema.type})` : ""}
    </p>
  );
}
