import { useCallback, useEffect, useRef, useState } from "react";
import type { OperatorApi } from "./api";
import type {
  MachineEventReceipt,
  MachineInstance,
  MachinePage,
  MachineSpec,
  MachineTransition,
  MachineVisitEvent,
} from "./machine";
import { MachinePayloadForm, machineButton } from "./MachinePayloadForm";
import { MachineVisitWorkspace } from "./MachineVisitWorkspace";

function useRead<T>(load: ((signal: AbortSignal) => Promise<T>) | undefined, poll = false) {
  const [result, setResult] = useState<{ load: typeof load; value: T }>();
  const [failure, setFailure] = useState<{ load: typeof load; error: string }>();
  const value = result?.load === load ? result?.value : undefined;
  const error = failure?.load === load ? failure?.error : undefined;
  const [revision, refresh] = useState(0);
  useEffect(() => {
    if (!load) return;
    const readValue = load;
    const controller = new AbortController();
    let timeout: ReturnType<typeof setTimeout>;
    async function read() {
      try {
        const next = await readValue(controller.signal);
        if (!controller.signal.aborted) {
          setResult({ load, value: next });
          setFailure(undefined);
        }
      } catch (cause) {
        if (!controller.signal.aborted)
          setFailure({ load, error: cause instanceof Error ? cause.message : "Read failed" });
      } finally {
        if (poll && !controller.signal.aborted) timeout = setTimeout(() => void read(), 1000);
      }
    }
    void read();
    return () => {
      controller.abort();
      clearTimeout(timeout);
    };
  }, [load, poll, revision]);
  return { value, error, refresh: () => refresh((current) => current + 1) };
}

function ReadError({ error, retry }: { error?: string; retry: () => void }) {
  return error ? (
    <p role="alert" className="text-xs text-danger">
      {error}{" "}
      <button className="underline" onClick={retry}>
        Retry read
      </button>
    </p>
  ) : null;
}
function clock(value: number | null) {
  return value === null ? "Not recorded" : new Date(value * 1000).toLocaleString();
}
function keyLabel(instance: MachineInstance) {
  return instance.key
    .map((part) => (typeof part === "string" ? part : JSON.stringify(part)))
    .join(" / ");
}

function EventFacts({
  api,
  runId,
  visit,
  live,
}: {
  api: OperatorApi;
  runId: string;
  visit: number;
  live: boolean;
}) {
  const [cursor, setCursor] = useState<string>();
  const load = useCallback(
    (signal: AbortSignal): Promise<MachinePage<MachineVisitEvent>> =>
      api.getMachineVisitEvents!(runId, visit, { cursor, limit: 25 }, signal),
    [api, runId, visit, cursor],
  );
  const read = useRead(
    api.getMachineVisitEvents ? load : undefined,
    live && cursor === undefined,
  );
  if (!api.getMachineVisitEvents)
    return (
      <p className="text-xs text-secondary">Event facts are unavailable from this host.</p>
    );
  return (
    <section aria-label="Event facts" className="grid gap-2">
      <h4 className="text-sm font-semibold">Event facts</h4>
      <ReadError error={read.error} retry={read.refresh} />
      {read.value ? (
        <>
          <div className="overflow-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Event</th>
                  <th>Disposition</th>
                  <th>Receipt</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {read.value.items.map((fact) => (
                  <tr key={fact.tseq} data-event-status={fact.status}>
                    <td className="py-2">{clock(fact.at)}</td>
                    <td>{fact.event ?? "—"}</td>
                    <td>{fact.status}</td>
                    <td className="font-mono">{fact.receipt_id ?? "—"}</td>
                    <td>{fact.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {read.value.items.length === 0 && (
            <p className="text-xs text-secondary">No event facts in this window.</p>
          )}
          <div className="flex gap-2">
            {cursor && (
              <button className={machineButton} onClick={() => setCursor(undefined)}>
                First facts
              </button>
            )}
            {read.value.next_cursor !== null && (
              <button
                className={machineButton}
                onClick={() => setCursor(read.value!.next_cursor!)}
              >
                Next facts
              </button>
            )}
          </div>
        </>
      ) : (
        <p role="status">Loading event facts…</p>
      )}
    </section>
  );
}

function VisitSection({
  api,
  runId,
  number,
  current,
  refreshKey,
  enabled,
  open,
  onToggle,
}: {
  api: OperatorApi;
  runId: string;
  number: number;
  current: boolean;
  refreshKey: string;
  enabled: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  const load = useCallback(
    (signal: AbortSignal) => api.getMachineVisit!(runId, number, signal),
    // refreshKey re-reads the visit after the instance moves on.
    [api, runId, number, refreshKey],
  );
  const read = useRead(load, current);
  const visit = read.value;
  return (
    <section
      id={`machine-visit-section-${number}`}
      className="border-t border-line py-3"
      data-visit={number}
      data-visit-state={visit?.state}
    >
      <button
        type="button"
        className="flex w-full items-center justify-between gap-3 text-left"
        aria-expanded={open}
        aria-controls={`machine-visit-${number}`}
        onClick={onToggle}
      >
        <span className="flex min-w-0 items-center gap-2 font-mono text-xs text-ink">
          <span className="text-secondary">Visit {number}</span>
          {visit && <strong className="truncate">{visit.state}</strong>}
          {visit && (
            <span className="text-[9px] tracking-[.12em] text-secondary uppercase">
              {visit.disposition}
            </span>
          )}
        </span>
        <span aria-hidden="true" className="text-secondary">
          {open ? "−" : "+"}
        </span>
      </button>
      <ReadError error={read.error} retry={read.refresh} />
      {visit && (
        <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 font-mono text-[10px] text-secondary">
          <dt>Entered</dt>
          <dd>{clock(visit.entered_at)}</dd>
          <dt>Left</dt>
          <dd>{clock(visit.left_at)}</dd>
          <dt>Catalog revision</dt>
          <dd>{visit.catalog_revision ?? "Not reported by this host"}</dd>
          {visit.transition && (
            <>
              <dt>Transition</dt>
              <dd>
                {visit.transition.from} → {visit.transition.to ?? "closed"} ·{" "}
                {visit.transition.event ?? visit.transition.source}
              </dd>
            </>
          )}
        </dl>
      )}
      {open && (
        <div id={`machine-visit-${number}`} className="mt-3 grid gap-4">
          {visit ? (
            <MachineVisitWorkspace
              api={api}
              runId={runId}
              visit={visit}
              enabled={enabled}
              current={current}
            />
          ) : (
            <p role="status">Loading visit…</p>
          )}
          <EventFacts api={api} runId={runId} visit={number} live={current} />
        </div>
      )}
    </section>
  );
}

function Timer({ due }: { due: number }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 250);
    return () => clearInterval(timer);
  }, []);
  return (
    <span className="text-secondary">
      Next timer: <time dateTime={new Date(due * 1000).toISOString()}>{clock(due)}</time> ·{" "}
      <span className="text-amber" data-timer-remaining={Math.max(0, Math.ceil(due - now))}>
        {due > now ? `${Math.ceil(due - now)}s remaining` : "Due — awaiting host"}
      </span>
    </span>
  );
}

/** The run's machine instance, polled while the page is open. */
export function useMachineInstance(api: OperatorApi, runId: string | undefined) {
  const load = useCallback(
    (signal: AbortSignal) => api.getMachineInstance!(runId!, signal),
    [api, runId],
  );
  return useRead(api.getMachineInstance && runId ? load : undefined, true);
}

/**
 * Below the shared graph on a machine run page: machine-only writes (send, retry,
 * dev timer), pending events and receipts, and one section per visit with that
 * visit's step graph, inspector, logs and waits. Cancel, logs and live status stay
 * the normal run controls.
 */
export function MachineRunSections({
  api,
  runId,
  spec: catalogSpec,
  instance,
  readError,
  refresh,
  enabled,
  focus,
}: {
  api: OperatorApi;
  runId: string;
  spec: MachineSpec;
  instance: MachineInstance | undefined;
  readError?: string;
  refresh: () => void;
  enabled: boolean;
  /** A state picked on the graph; its latest visit opens. */
  focus?: { state: string; nonce: number };
}) {
  const spec = instance?.machine_spec ?? catalogSpec;
  const [cursor, setCursor] = useState<string>();
  const historyLoad = useCallback(
    (signal: AbortSignal): Promise<MachinePage<MachineTransition>> =>
      api.getMachineHistory!(runId, { cursor, limit: 25 }, signal),
    [api, runId, cursor],
  );
  const history = useRead(
    api.getMachineHistory ? historyLoad : undefined,
    cursor === undefined,
  );
  const [openVisits, setOpenVisits] = useState<ReadonlySet<number>>(() => new Set());
  const [event, setEvent] = useState<string>();
  const [receipt, setReceipt] = useState<MachineEventReceipt>();
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string>();
  const idempotency = useRef<{ signature: string; key: string } | undefined>(undefined);
  const actionScope = useRef(true);
  useEffect(() => {
    actionScope.current = true;
    return () => {
      actionScope.current = false;
    };
  }, []);
  const handledFocus = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!focus || !instance || handledFocus.current === focus.nonce) return;
    handledFocus.current = focus.nonce;
    const visit =
      focus.state === instance.state
        ? instance.visit
        : Math.max(
            0,
            ...(history.value?.items ?? [])
              .filter((transition) => transition.from === focus.state)
              .map((transition) => transition.visit),
          );
    if (visit <= 0) return;
    setOpenVisits((current) => new Set(current).add(visit));
    requestAnimationFrame(() =>
      document
        .getElementById(`machine-visit-section-${visit}`)
        ?.scrollIntoView({ block: "start", behavior: "smooth" }),
    );
  }, [focus, history.value, instance]);
  async function action(mutate: () => Promise<void>) {
    setBusy(true);
    setActionError(undefined);
    try {
      await mutate();
      if (actionScope.current) {
        refresh();
        history.refresh();
      }
    } catch (cause) {
      if (actionScope.current)
        setActionError(cause instanceof Error ? cause.message : "Action failed");
    } finally {
      if (actionScope.current) setBusy(false);
    }
  }
  if (!api.getMachineInstance)
    return (
      <p role="status" className="p-4 text-xs text-secondary">
        Machine reads are unavailable from this host.
      </p>
    );
  if (!instance)
    return (
      <div className="p-4 text-xs">
        <ReadError error={readError} retry={refresh} />
        <p role="status">Loading machine run…</p>
      </div>
    );
  const events = new Map<string, MachineSpec["states"][string]["transitions"][number]>();
  for (const state of Object.values(spec.states))
    for (const transition of state.transitions)
      if (!events.has(transition.event)) events.set(transition.event, transition);
  // Event names may recur with state-specific payload schemas.
  for (const transition of (instance.state && spec.states[instance.state]?.transitions) || [])
    events.set(transition.event, transition);
  const chosen = event && events.has(event) ? event : events.keys().next().value;
  const visitNumbers = [
    ...new Set([
      ...(history.value?.items.map((transition) => transition.visit) ?? []),
      instance.visit,
    ]),
  ]
    .filter((number) => number > 0)
    .sort((a, b) => b - a);
  const writable =
    enabled &&
    !instance.finished &&
    Boolean(api.retryMachineVisit || api.fireNextMachineTimer || api.sendMachineEvent);
  return (
    <article
      aria-label="Machine run"
      className="grid gap-4 p-4 text-ink"
      data-machine-run={runId}
    >
      <header className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11px]">
        <strong className="text-sm">{keyLabel(instance)}</strong>
        <span className="text-secondary">
          State{" "}
          <span className="text-ink" data-machine-state={instance.state}>
            {instance.state ?? "none"}
          </span>
        </span>
        {instance.due !== null && <Timer due={instance.due} />}
        <ReadError error={readError} retry={refresh} />
      </header>
      {writable && (
        <section className="grid gap-3" aria-label="Machine actions">
          <div className="flex flex-wrap gap-2">
            {api.retryMachineVisit && (
              <button
                type="button"
                className={machineButton}
                disabled={
                  busy ||
                  !["interrupted", "needs_attention", "indeterminate"].includes(instance.status)
                }
                onClick={() => void action(() => api.retryMachineVisit!(runId))}
              >
                Retry visit
              </button>
            )}
            {api.fireNextMachineTimer && instance.due !== null && (
              <button
                type="button"
                className={machineButton}
                disabled={busy}
                title="Development hosts only"
                onClick={() => void action(() => api.fireNextMachineTimer!(runId))}
              >
                Fire next timer
              </button>
            )}
          </div>
          {actionError && (
            <p role="alert" className="text-xs text-danger">
              {actionError}
            </p>
          )}
          {api.sendMachineEvent && chosen && (
            <details className="rounded border border-line bg-panel p-3">
              <summary className="cursor-pointer text-xs">Send event</summary>
              <div className="mt-3 grid max-w-lg gap-3">
                <label className="grid gap-1 text-xs">
                  Event
                  <select
                    aria-label="Event"
                    className="rounded border border-line bg-canvas p-2"
                    value={chosen}
                    onChange={(e) => setEvent(e.target.value)}
                  >
                    {[...events.keys()].map((name) => (
                      <option key={name}>{name}</option>
                    ))}
                  </select>
                </label>
                <p className="text-xs text-secondary">
                  Accepts now: {instance.accepts.join(", ") || "None"}. Other events may be
                  deferred or ignored by the host.
                </p>
                <MachinePayloadForm
                  key={`${chosen}:${JSON.stringify(events.get(chosen)!.payload_schema)}`}
                  schema={events.get(chosen)!.payload_schema}
                  label="Send event"
                  disabled={busy}
                  onSubmit={async (payload) => {
                    const signature = JSON.stringify([chosen, payload]);
                    if (idempotency.current?.signature !== signature)
                      idempotency.current = {
                        signature,
                        // getRandomValues also works for an HTTP operator on a LAN host.
                        key: Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
                          byte.toString(16).padStart(2, "0"),
                        ).join(""),
                      };
                    const result = await api.sendMachineEvent!(
                      runId,
                      chosen,
                      payload,
                      idempotency.current.key,
                    );
                    if (!actionScope.current) return;
                    setReceipt(result);
                    idempotency.current = undefined;
                    refresh();
                    history.refresh();
                  }}
                />
              </div>
            </details>
          )}
        </section>
      )}
      <section aria-label="Pending events and receipts" className="grid gap-1 text-xs">
        <h3 className="font-mono text-[9px] tracking-[.12em] text-secondary uppercase">
          Pending events and receipts
        </h3>
        {instance.pending_events === undefined ? (
          <p className="text-secondary">Pending inbox not reported by this host.</p>
        ) : instance.pending_events.length === 0 ? (
          <p className="text-secondary">No pending events.</p>
        ) : (
          instance.pending_events.map((pending) => (
            <p key={pending.receipt_id} className="font-mono text-[11px]">
              {pending.event} · {pending.receipt_id} ·{" "}
              <code>{JSON.stringify(pending.payload)}</code>
            </p>
          ))
        )}
        {[
          ...(instance.receipts ?? []).filter(
            (item) => item.receipt_id !== receipt?.receipt_id,
          ),
          ...(receipt ? [receipt] : []),
        ].map((item) => (
          <p
            key={item.receipt_id}
            className="font-mono text-[11px]"
            data-receipt-status={item.status}
            data-receipt-duplicate={item.duplicate}
          >
            {item.key} · {item.status}
            {item.duplicate ? " · duplicate" : ""} · {item.receipt_id} ·{" "}
            {clock(item.accepted_at)}
          </p>
        ))}
      </section>
      <section aria-label="Machine visits">
        <h3 className="font-mono text-[9px] tracking-[.12em] text-secondary uppercase">
          Visits
        </h3>
        {!api.getMachineHistory ? (
          <p className="text-xs text-secondary">History unavailable from this host.</p>
        ) : (
          <ReadError error={history.error} retry={history.refresh} />
        )}
        {!api.getMachineVisit ? (
          <p className="text-xs text-secondary">Visit detail unavailable from this host.</p>
        ) : (
          visitNumbers.map((number) => (
            <VisitSection
              key={number}
              api={api}
              runId={runId}
              number={number}
              current={number === instance.visit && !instance.finished}
              enabled={enabled}
              refreshKey={`${instance.visit}:${instance.status}`}
              open={openVisits.has(number)}
              onToggle={() =>
                setOpenVisits((current) => {
                  const next = new Set(current);
                  if (!next.delete(number)) next.add(number);
                  return next;
                })
              }
            />
          ))
        )}
        <div className="flex gap-2">
          {cursor && (
            <button
              type="button"
              className={machineButton}
              onClick={() => setCursor(undefined)}
            >
              Latest history
            </button>
          )}
          {history.value?.next_cursor != null && (
            <button
              type="button"
              className={machineButton}
              onClick={() => setCursor(history.value!.next_cursor!)}
            >
              Older visits
            </button>
          )}
        </div>
      </section>
    </article>
  );
}
