import { useEffect, useState } from "react";
import type { OperatorApi } from "./api";
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

function DecisionForm({
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

/** Uses pinned server schemas and server-side validation, never a payload reviewer ID. */
export function RunWaitPanel({
  api,
  runId,
  visit,
  readOnly = false,
  live = true,
}: {
  api: OperatorApi;
  runId: string;
  visit?: number;
  readOnly?: boolean;
  live?: boolean;
}) {
  const [cards, setCards] = useState<WaitCard[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!api.listRunWaits) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const next = await api.listRunWaits!(runId, visit);
        if (!stopped) {
          setCards(next);
          setError("");
        }
      } catch (cause) {
        if (!stopped) setError(cause instanceof Error ? cause.message : "Waits unavailable");
      }
      if (!stopped && live) timer = setTimeout(() => void load(), 1500);
    }
    void load();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [api, runId, visit, live]);
  // Nothing pending: render nothing, so the waits dock beside the graph collapses.
  if (!api.listRunWaits || (cards.length === 0 && !error)) return null;
  return (
    <section aria-label="Run waits" className="bg-panel p-3 text-xs">
      {cards.map((card) => (
        <article key={card.nodeId} className="mb-3 grid gap-2">
          <h3>
            {card.prompt ?? card.kind} · {card.nodeId} · {card.state}
          </h3>
          {card.show !== null && (
            <pre className="max-h-32 overflow-auto">{JSON.stringify(card.show, null, 2)}</pre>
          )}
          {card.audit && (
            <section
              aria-label="Decision audit"
              className="grid gap-1 border-t border-line pt-2"
            >
              <p>
                {card.audit.reason} · {new Date(card.audit.at * 1000).toLocaleString()}
              </p>
              <pre>{JSON.stringify(card.audit.outcome, null, 2)}</pre>
            </section>
          )}
          {!readOnly &&
            api.submitDecision &&
            card.kind === "decision" &&
            card.state === "waiting" && (
              <DecisionForm
                card={card}
                submit={async (outcome, reason, key) => {
                  await api.submitDecision!(runId, card.nodeId, outcome, reason, key);
                  setCards(await api.listRunWaits!(runId, visit));
                }}
              />
            )}
        </article>
      ))}
      {error && <p role="status">{error}</p>}
    </section>
  );
}
