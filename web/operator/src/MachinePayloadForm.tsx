import { useId, useState, type FormEvent } from "react";
import type { JsonSchema, JsonValue, Schema } from "./stepInterface";
import { parseJsonValue } from "./stepInterface";

export const machineButton =
  "rounded border border-line bg-panel px-3 py-2 text-xs text-ink disabled:opacity-50";

function resolve(schema: Schema, root: JsonSchema): Schema {
  if (typeof schema === "boolean" || !schema.$ref) return schema;
  const path = schema.$ref;
  if (!path.startsWith("#/$defs/")) return schema;
  return root.$defs?.[path.slice(8)] ?? schema;
}

function Field({
  name,
  schema: raw,
  root,
  required,
  value,
  onChange,
}: {
  name: string;
  schema: Schema;
  root: JsonSchema;
  required: boolean;
  value: string;
  onChange: (value: string) => void;
}) {
  const id = useId();
  const schema = resolve(raw, root);
  const property = typeof schema === "boolean" ? undefined : schema;
  const type = property?.type;
  const simple = type === "string" || type === "number" || type === "integer";
  const inputClass = "w-full rounded border border-line bg-canvas px-3 py-2 text-sm text-ink";
  return (
    <label htmlFor={id} className="grid gap-1 text-xs text-secondary">
      <span>
        {name}
        {required ? " *" : ""}
      </span>
      {property?.enum ? (
        <select
          id={id}
          className={inputClass}
          required={required}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">Choose a value</option>
          {property.enum.map((item) => (
            <option key={JSON.stringify(item)} value={JSON.stringify(item)}>
              {JSON.stringify(item)}
            </option>
          ))}
        </select>
      ) : type === "boolean" ? (
        <select
          id={id}
          className={inputClass}
          required={required}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">Not supplied</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      ) : simple ? (
        <input
          id={id}
          className={inputClass}
          required={required}
          type={type === "string" ? "text" : "number"}
          step={type === "integer" ? 1 : "any"}
          min={property?.minimum}
          max={property?.maximum}
          minLength={property?.minLength}
          maxLength={property?.maxLength}
          pattern={property?.pattern}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <textarea
          id={id}
          className={`${inputClass} font-mono`}
          required={required}
          value={value}
          rows={3}
          placeholder="JSON value"
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {property?.description && <span>{property.description}</span>}
    </label>
  );
}

/** Primitive fields get typed controls; nested/union values use JSON. The host validates the complete schema. */
export function MachinePayloadForm({
  schema,
  label,
  disabled,
  onSubmit,
}: {
  schema: JsonSchema | null;
  label: string;
  disabled?: boolean;
  onSubmit: (value: JsonValue) => Promise<void>;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const fields =
    schema?.type === "object" && schema.properties
      ? Object.entries(schema.properties)
      : schema
        ? [["payload", schema] as const]
        : [];
  function text(name: string, field: Schema): string {
    if (values[name] !== undefined) return values[name];
    const property = resolve(field, schema!);
    if (typeof property === "boolean" || property.default === undefined) return "";
    return property.type === "string" && !property.enum && typeof property.default === "string"
      ? property.default
      : JSON.stringify(property.default);
  }
  function parse(name: string, field: Schema): JsonValue | undefined {
    const value = text(name, field);
    if (value === "") return undefined;
    const property = resolve(field, schema!);
    return typeof property !== "boolean" && property.type === "string" && !property.enum
      ? value
      : parseJsonValue(JSON.parse(value), name);
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(undefined);
    setBusy(true);
    try {
      const payload: JsonValue =
        schema === null
          ? null
          : schema.type === "object" && schema.properties
            ? Object.fromEntries(
                fields.flatMap(([name, field]) => {
                  const value = parse(name, field);
                  return value === undefined ? [] : [[name, value]];
                }),
              )
            : (parse("payload", schema) ?? null);
      await onSubmit(payload);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The request failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="grid gap-3" onSubmit={(event) => void submit(event)} aria-label={label}>
      <fieldset disabled={disabled || busy} className="grid gap-3">
        {fields.map(([name, field]) => (
          <Field
            key={name}
            name={name}
            schema={field}
            root={schema!}
            required={
              schema?.type === "object" ? schema.required?.includes(name) === true : true
            }
            value={text(name, field)}
            onChange={(value) => setValues((current) => ({ ...current, [name]: value }))}
          />
        ))}
        <button className={machineButton} type="submit">
          {busy ? "Submitting…" : label}
        </button>
      </fieldset>
      {error && (
        <p role="alert" className="text-xs text-danger">
          {error}
        </p>
      )}
    </form>
  );
}
