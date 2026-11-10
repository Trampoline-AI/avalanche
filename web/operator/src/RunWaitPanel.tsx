import { useState } from "react";
import type { JsonSchema, JsonValue, Schema } from "./stepInterface";

export interface WaitCard {
  nodeId: string;
  kind: "sleep" | "event" | "decision";
  state: "pending" | "waiting" | "resolved" | "withdrawn";
  schema: JsonSchema;
  prompt: string | null;
  show: JsonValue;
  /** Retained terminal decision evidence, supplied by the host. */
  audit?: { outcome: JsonValue; reason: string; at: number };
}

/** A decision's pinned artifact or audit outcome, as indented JSON. */
export function JsonBlock({ label, value }: { label: string; value: JsonValue }) {
  return (
    <section aria-label={label} className="grid gap-1">
      <span className="text-[8px] tracking-[.12em] text-secondary uppercase">{label}</span>
      <pre className="m-0 max-h-40 overflow-auto rounded border border-line bg-canvas-shell p-1.5 text-[10px] whitespace-pre-wrap text-ink [overflow-wrap:anywhere]">
        {JSON.stringify(value, null, 2)}
      </pre>
    </section>
  );
}

/**
 * Answers a pending decision wait, showing the artifact under review first (never
 * answered blind). Uses the pinned server schema and server-side validation, never
 * a payload reviewer ID; the idempotency key is kept for retries.
 */
export function DecisionForm({
  card,
  submit,
}: {
  card: WaitCard;
  submit: (outcome: Record<string, unknown>, reason: string, key: string) => Promise<void>;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [key] = useState(() =>
    Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join(""),
  );
  const properties = card.schema.properties ?? {};
  async function decide(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const outcome: Record<string, unknown> = {};
      for (const [name, raw] of Object.entries(values)) {
        const schema: Schema = properties[name];
        outcome[name] =
          typeof schema !== "boolean" && schema.type === "string" && !schema.enum
            ? raw
            : JSON.parse(raw);
      }
      await submit(outcome, reason, key);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Decision could not be accepted");
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={(event) => void decide(event)} className="grid gap-2">
      {card.show !== null && card.show !== undefined && (
        <JsonBlock label="Reviewed artifact" value={card.show} />
      )}
      {Object.entries(properties).map(([name, schema]) => {
        const field = typeof schema === "boolean" ? undefined : schema;
        return (
          <label key={name} className="grid gap-1">
            {field?.title ?? name}
            {field?.enum ? (
              <select
                aria-label={name}
                value={values[name] ?? ""}
                required={card.schema.required?.includes(name)}
                onChange={(event) => setValues({ ...values, [name]: event.target.value })}
              >
                <option value="">Choose…</option>
                {field.enum.map((choice) => (
                  <option key={JSON.stringify(choice)} value={JSON.stringify(choice)}>
                    {typeof choice === "string" ? choice : JSON.stringify(choice)}
                  </option>
                ))}
              </select>
            ) : (
              <input
                aria-label={name}
                value={values[name] ?? ""}
                placeholder={field?.type === "string" ? "Text" : "JSON value"}
                required={card.schema.required?.includes(name)}
                onChange={(event) => setValues({ ...values, [name]: event.target.value })}
              />
            )}
          </label>
        );
      })}
      <label className="grid gap-1">
        Reason
        <input value={reason} required onChange={(event) => setReason(event.target.value)} />
      </label>
      <button type="submit" disabled={busy || !reason.trim()}>
        {busy ? "Submitting…" : "Submit decision"}
      </button>
      {error && <p role="alert">{error}</p>}
    </form>
  );
}
