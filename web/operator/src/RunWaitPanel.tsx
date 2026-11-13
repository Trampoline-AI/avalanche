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
  // Hosts may store schema keys in any order; ask required fields first, then the rest.
  const required = card.schema.required ?? [];
  const fields = Object.entries(properties).sort(
    ([a], [b]) => Number(required.includes(b)) - Number(required.includes(a)),
  );
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
    <form onSubmit={(event) => void decide(event)} className="grid max-w-xl gap-2.5 pt-1">
      {card.show !== null && card.show !== undefined && (
        <JsonBlock label="Reviewed artifact" value={card.show} />
      )}
      {fields.map(([name, schema]) => {
        const field = typeof schema === "boolean" ? undefined : schema;
        return (
          <label key={name} className="grid gap-1 text-[10px] font-medium text-ink">
            {field?.title ?? name}
            {field?.enum ? (
              <select
                aria-label={name}
                className="h-8 min-w-0 rounded border border-line bg-panel px-2 text-[10px] font-normal text-ink outline-acid placeholder:text-input-muted"
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
                className="h-8 min-w-0 rounded border border-line bg-panel px-2 text-[10px] font-normal text-ink outline-acid placeholder:text-input-muted"
                value={values[name] ?? ""}
                placeholder={field?.type === "string" ? "Text" : "JSON value"}
                required={card.schema.required?.includes(name)}
                onChange={(event) => setValues({ ...values, [name]: event.target.value })}
              />
            )}
          </label>
        );
      })}
      <label className="grid gap-1 text-[10px] font-medium text-ink">
        Reason
        <input
          className="h-8 min-w-0 rounded border border-line bg-panel px-2 text-[10px] font-normal text-ink outline-acid placeholder:text-input-muted"
          value={reason}
          placeholder="Why this decision (recorded in the audit)"
          required
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      <button
        type="submit"
        className="cursor-pointer justify-self-start rounded border border-acid bg-acid px-3 py-1.5 text-[10px] font-medium text-panel hover:bg-acid-hover disabled:cursor-not-allowed disabled:opacity-50"
        disabled={busy || !reason.trim()}
      >
        {busy ? "Submitting…" : "Submit decision"}
      </button>
      {error && (
        <p role="alert" className="m-0 text-[10px] text-cancel-ink">
          {error}
        </p>
      )}
    </form>
  );
}
