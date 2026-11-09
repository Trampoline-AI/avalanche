import { useCallback, useEffect, useRef, useState } from "react";
import type { OperatorApi } from "./api";
import type { FlowInfoMsg } from "./model";
import type {
  JsonValue,
  MachineEventReceipt,
  MachineInstance,
  MachinePage,
  MachineSpec,
  MachineTransition,
  MachineVisitEvent,
} from "./machine";
import { GraphCanvas } from "./GraphCanvas";
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
}: {
  api: OperatorApi;
  runId: string;
  number: number;
  current: boolean;
  refreshKey: string;
}) {
  const [open, setOpen] = useState(false);
  const load = useCallback(
    (signal: AbortSignal) => api.getMachineVisit!(runId, number, signal),
    [api, runId, number, refreshKey],
  );
  const read = useRead(load, current);
  const visit = read.value;
  return (
    <section className="border-t border-line py-4" data-visit={number}>
      <button
        className="flex w-full items-center justify-between text-left text-sm font-semibold"
        aria-expanded={open}
        aria-controls={`machine-visit-${number}`}
        onClick={() => setOpen(!open)}
      >
        <span>
          Visit {number}
          {visit ? ` · ${visit.state}` : ""}
        </span>
        <span aria-hidden="true">{open ? "−" : "+"}</span>
      </button>
      <ReadError error={read.error} retry={read.refresh} />
      {visit && (
        <div className="mt-2 grid gap-1 text-xs text-secondary">
          <p>
            {visit.disposition} · Entered {clock(visit.entered_at)} · Left{" "}
            {clock(visit.left_at)}
          </p>
          <p>Catalog revision: {visit.catalog_revision ?? "Not reported by this host"}</p>
          {visit.transition && (
            <p>
              Transition: {visit.transition.from} → {visit.transition.to ?? "closed"} ·{" "}
              {visit.transition.event ?? visit.transition.source}
            </p>
          )}
        </div>
      )}
      {open && (
        <div id={`machine-visit-${number}`} className="mt-4 grid gap-5">
          {visit ? (
            <MachineVisitWorkspace api={api} runId={runId} visit={visit} />
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
    <p className="text-xs text-secondary">
      Next timer: <time dateTime={new Date(due * 1000).toISOString()}>{clock(due)}</time> ·{" "}
      <span data-timer-remaining={Math.max(0, Math.ceil(due - now))}>
        {due > now ? `${Math.ceil(due - now)}s remaining` : "Due — awaiting host"}
      </span>
    </p>
  );
}

function MachineRun({
  api,
  runId,
  spec: catalogSpec,
  enabled,
}: {
  api: OperatorApi;
  runId: string;
  spec: MachineSpec;
  enabled: boolean;
}) {
  const load = useCallback(
    (signal: AbortSignal) => api.getMachineInstance!(runId, signal),
    [api, runId],
  );
  const read = useRead(api.getMachineInstance ? load : undefined, true);
  const instance = read.value;
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
  async function action(mutate: () => Promise<void>) {
    setBusy(true);
    setActionError(undefined);
    try {
      await mutate();
      if (actionScope.current) {
        read.refresh();
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
    return <p role="status">Machine reads are unavailable from this host.</p>;
  if (!instance)
    return (
      <>
        <ReadError error={read.error} retry={read.refresh} />
        <p role="status">Loading machine run…</p>
      </>
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
  return (
    <article
      aria-label="Machine run"
      className="min-w-0 flex-1 overflow-auto p-5 text-ink"
      data-machine-run={runId}
    >
      <header className="grid gap-2">
        <h2 className="text-lg font-semibold">{keyLabel(instance)}</h2>
        <p className="text-xs text-secondary">
          <span className="font-mono">{runId}</span> ·{" "}
          <span data-machine-state={instance.state}>
            {instance.state ?? "No current state"}
          </span>{" "}
          · {instance.status}
        </p>
        <ReadError error={read.error} retry={read.refresh} />
        {instance.due !== null && <Timer due={instance.due} />}
      </header>
      <div className="my-4 h-[380px] min-w-0 rounded border border-line bg-canvas">
        <GraphCanvas machine={{ spec, state: instance.state }} onOpenNode={() => {}} />
      </div>
      {enabled &&
        !instance.finished &&
        (api.retryMachineVisit ||
          api.closeMachine ||
          api.fireNextMachineTimer ||
          api.sendMachineEvent) && (
          <section className="mb-5 grid gap-3" aria-label="Machine actions">
            <div className="flex flex-wrap gap-2">
              {api.retryMachineVisit && (
                <button
                  className={machineButton}
                  disabled={
                    busy ||
                    !["interrupted", "needs_attention", "indeterminate"].includes(
                      instance.status,
                    )
                  }
                  onClick={() => void action(() => api.retryMachineVisit!(runId))}
                >
                  Retry visit
                </button>
              )}
              {api.closeMachine && (
                <button
                  className={machineButton}
                  disabled={busy}
                  onClick={() => {
                    if (window.confirm("Close this machine run?"))
                      void action(() => api.closeMachine!(runId));
                  }}
                >
                  Close machine
                </button>
              )}
              {api.fireNextMachineTimer && instance.due !== null && (
                <button
                  className={machineButton}
                  disabled={busy}
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
              <details className="rounded border border-line p-3">
                <summary className="cursor-pointer text-sm">Send event</summary>
                <div className="mt-3 grid max-w-lg gap-3">
                  <label className="grid gap-1 text-xs">
                    Event
                    <select
                      aria-label="Event"
                      className="rounded border border-line bg-panel p-2"
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
                      read.refresh();
                      history.refresh();
                    }}
                  />
                </div>
              </details>
            )}
          </section>
        )}
      <section
        aria-label="Pending events and receipts"
        className="mb-5 grid gap-2 border-t border-line pt-4 text-xs"
      >
        <h3 className="text-sm font-semibold">Pending events and receipts</h3>
        {instance.pending_events === undefined ? (
          <p className="text-secondary">Pending inbox not reported by this host.</p>
        ) : instance.pending_events.length === 0 ? (
          <p className="text-secondary">No pending events.</p>
        ) : (
          instance.pending_events.map((pending) => (
            <p key={pending.receipt_id}>
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
        <h3 className="text-sm font-semibold">Visits</h3>
        {!api.getMachineHistory ? (
          <p className="text-xs text-secondary">History unavailable from this host.</p>
        ) : (
          <ReadError error={history.error} retry={history.refresh} />
        )}
        {!api.getMachineVisit ? (
          <p className="text-xs text-secondary">Visit graphs unavailable from this host.</p>
        ) : (
          visitNumbers.map((number) => (
            <VisitSection
              key={number}
              api={api}
              runId={runId}
              number={number}
              current={number === instance.visit && !instance.finished}
              refreshKey={`${instance.visit}:${instance.status}`}
            />
          ))
        )}
        <div className="flex gap-2">
          {cursor && (
            <button className={machineButton} onClick={() => setCursor(undefined)}>
              Latest history
            </button>
          )}
          {history.value?.next_cursor != null && (
            <button
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

export function MachineWorkspace({
  api,
  workflow,
  selectedRunId,
  onSelectRun,
  enabled,
  className = "",
}: {
  api: OperatorApi;
  workflow: FlowInfoMsg;
  selectedRunId?: string;
  onSelectRun: (id?: string) => void;
  enabled: boolean;
  className?: string;
}) {
  const spec = workflow.machineSpec!;
  const [cursor, setCursor] = useState<string>();
  const load = useCallback(
    (signal: AbortSignal): Promise<MachinePage<MachineInstance>> =>
      api.listMachineInstances!(spec.name, { cursor, limit: 25 }, signal),
    [api, spec.name, cursor],
  );
  const read = useRead(api.listMachineInstances ? load : undefined, true);
  const navigation = useRef({ selectedRunId, machine: spec.name, generation: 0 });
  if (
    navigation.current.selectedRunId !== selectedRunId ||
    navigation.current.machine !== spec.name
  )
    navigation.current = {
      selectedRunId,
      machine: spec.name,
      generation: navigation.current.generation + 1,
    };
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return (
    <section
      className={`avalanche-operator-ui avalanche-workspace flex h-full min-h-0 min-w-0 bg-canvas ${className}`}
    >
      <aside
        className="w-[270px] shrink-0 overflow-auto border-r border-line bg-panel p-4"
        aria-label="Machine run list"
      >
        <h2 className="mb-3 text-sm font-semibold text-ink">Runs</h2>
        <button
          className={`${machineButton} mb-3 w-full`}
          aria-current={!selectedRunId ? "page" : undefined}
          onClick={() => onSelectRun(undefined)}
        >
          Current definition
        </button>
        {!api.listMachineInstances ? (
          <p className="text-xs text-secondary">
            Machine run listing is unavailable from this host.
          </p>
        ) : (
          <>
            <ReadError error={read.error} retry={read.refresh} />
            {read.value?.items.map((instance) => (
              <button
                key={instance.run_id}
                data-machine-run-entry={instance.run_id}
                aria-current={selectedRunId === instance.run_id ? "page" : undefined}
                className={`mb-2 grid w-full gap-1 rounded border p-3 text-left text-xs ${selectedRunId === instance.run_id ? "border-acid bg-acid/10" : "border-line"}`}
                onClick={() => onSelectRun(instance.run_id)}
              >
                <strong className="text-ink">{keyLabel(instance)}</strong>
                <span className="text-secondary">
                  {instance.state} · {instance.status}
                </span>
                <span className="truncate font-mono text-secondary">{instance.run_id}</span>
              </button>
            ))}
            <div className="flex gap-2">
              {cursor && (
                <button className={machineButton} onClick={() => setCursor(undefined)}>
                  First runs
                </button>
              )}
              {read.value?.next_cursor != null && (
                <button
                  className={machineButton}
                  onClick={() => setCursor(read.value!.next_cursor!)}
                >
                  Next runs
                </button>
              )}
            </div>
          </>
        )}
      </aside>
      {selectedRunId ? (
        <MachineRun
          key={selectedRunId}
          api={api}
          runId={selectedRunId}
          spec={spec}
          enabled={enabled}
        />
      ) : (
        <article className="min-w-0 flex-1 overflow-auto p-5 text-ink">
          <h2 className="text-lg font-semibold">{workflow.displayName || workflow.name}</h2>
          <div className="my-4 h-[400px] rounded border border-line">
            <GraphCanvas machine={{ spec }} onOpenNode={() => {}} />
          </div>
          {enabled && api.startMachine && (
            <section className="max-w-lg" aria-label="Start machine">
              <h3 className="mb-3 text-sm font-semibold">Start machine</h3>
              <p className="mb-3 text-xs text-secondary">
                Key fields: {spec.key_fields.join(", ")}. Starting the same key opens the same
                run.
              </p>
              <MachinePayloadForm
                schema={spec.input_schema}
                label="Start machine"
                onSubmit={async (input) => {
                  if (input === null || typeof input !== "object" || Array.isArray(input))
                    throw new Error("Machine input must be an object");
                  const key: JsonValue[] = spec.key_fields.map((field) => {
                    if (!(field in input)) throw new Error(`Key field ${field} is required`);
                    return input[field];
                  });
                  const generation = navigation.current.generation;
                  const result = await api.startMachine!(spec.name, { key, input });
                  if (alive.current && generation === navigation.current.generation) {
                    read.refresh();
                    onSelectRun(result.run_id);
                  }
                }}
              />
            </section>
          )}
        </article>
      )}
    </section>
  );
}
