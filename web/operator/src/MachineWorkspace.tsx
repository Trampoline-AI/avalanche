import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import type { OperatorApi } from "./api";
import type {
  MachineEventReceipt,
  MachineInstance,
  MachinePage,
  MachineSpec,
  MachineTransition,
  MachineVisit,
  MachineVisitEvent,
} from "./machine";
import type { FlowInfoMsg, InputFileAttachment } from "./model";
import { parseRunInput } from "./RunControls";
import { runKeyLabel } from "./runKey";
import { parseJsonValue, type JsonSchema, type JsonValue } from "./stepInterface";
import { applyInputFiles, useInputDraft } from "./useInputDraft";
import { InputFieldList } from "./WorkflowInputInspector";
import { workflowInputFields } from "./workflowInput";
import { RunWaitPanel } from "./RunWaitPanel";

const machineButton =
  "rounded border border-line bg-panel px-3 py-2 text-xs text-ink disabled:opacity-50";

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
      <h3 className="m-0 border-b border-line pb-2 font-mono text-[9px] tracking-[.16em] text-secondary uppercase">
        Event facts
      </h3>
      <ReadError error={read.error} retry={read.refresh} />
      {read.value ? (
        <>
          <ul className="m-0 grid list-none gap-1 p-0 font-mono text-[11px]">
            {read.value.items.map((fact) => (
              <li key={fact.tseq} data-event-status={fact.status}>
                <span className="text-ink">{fact.event ?? "—"}</span> · {fact.status} ·{" "}
                <span className="text-secondary">
                  {fact.source} · {fact.receipt_id ?? "no receipt"} · {clock(fact.at)}
                </span>
              </li>
            ))}
          </ul>
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

function Timer({ due }: { due: number }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 250);
    return () => clearInterval(timer);
  }, []);
  return (
    <span>
      <time dateTime={new Date(due * 1000).toISOString()}>{clock(due)}</time> ·{" "}
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

/** The newest window of the run's transitions; polled while it is the first window. */
export function useMachineHistory(api: OperatorApi, runId: string | undefined) {
  const [cursor, setCursor] = useState<{ runId?: string; value?: string }>();
  const current = cursor && cursor.runId === runId ? cursor.value : undefined;
  const load = useCallback(
    (signal: AbortSignal): Promise<MachinePage<MachineTransition>> =>
      api.getMachineHistory!(runId!, { cursor: current, limit: 25 }, signal),
    [api, runId, current],
  );
  const read = useRead(
    api.getMachineHistory && runId ? load : undefined,
    current === undefined,
  );
  return {
    ...read,
    cursor: current,
    setCursor: (value: string | undefined) => setCursor({ runId, value }),
  };
}
export type MachineHistory = ReturnType<typeof useMachineHistory>;

/** One visit, polled while it is the instance's current visit. */
export function useMachineVisit(
  api: OperatorApi,
  runId: string | undefined,
  visit: number | undefined,
  live: boolean,
  refreshKey: string,
) {
  const load = useCallback(
    (signal: AbortSignal) => api.getMachineVisit!(runId!, visit!, signal),
    // refreshKey re-reads the visit after the instance moves on.
    [api, runId, visit, refreshKey],
  );
  return useRead(api.getMachineVisit && runId && visit !== undefined ? load : undefined, live);
}

/** The state each visit ran in, newest first, from the instance and its history. */
export function machineVisits(
  instance: MachineInstance,
  history: MachinePage<MachineTransition> | undefined,
): { visit: number; state: string; left?: MachineTransition }[] {
  const visits = new Map<number, { visit: number; state: string; left?: MachineTransition }>();
  for (const transition of history?.items ?? [])
    visits.set(transition.visit, {
      visit: transition.visit,
      state: transition.from,
      left: transition,
    });
  if (instance.visit > 0 && instance.state && !visits.has(instance.visit))
    visits.set(instance.visit, { visit: instance.visit, state: instance.state });
  return [...visits.values()].sort((a, b) => b.visit - a.visit);
}

/**
 * The visits of the selected machine run, as a section of the run timeline. The
 * first row returns to the state graph; each visit row opens that visit's steps.
 */
export function MachineVisitTimeline({
  instance,
  history,
  selectedVisit,
  onSelectVisit,
  expanded = false,
}: {
  instance: MachineInstance | undefined;
  history: MachineHistory;
  selectedVisit?: number;
  onSelectVisit: (visit: number | undefined) => void;
  expanded?: boolean;
}) {
  if (!instance) return null;
  const visits = machineVisits(instance, history.value);
  const row = `machine-visit-row flex w-full cursor-pointer items-center justify-between gap-3 border-0 border-b border-run-list-line text-left font-mono leading-none ${expanded ? "h-[34px] pl-[28px] pr-[30px] text-[11px]" : "h-[27.5px] pl-[28px] pr-[30px] text-[10px]"}`;
  const active =
    "active bg-selected shadow-[inset_2px_0_var(--color-acid)] hover:bg-selected-hover";
  return (
    <section aria-label="Machine visits" className="shrink-0 border-t border-run-list-line">
      <div className="flex h-7 items-center justify-between pl-[16px] pr-[30px] font-mono text-[9px] tracking-[0.16em] text-run-status-muted uppercase">
        <span>Visits</span>
        <span>{runKeyLabel(instance.key)}</span>
      </div>
      <div className={`overflow-auto overscroll-contain ${expanded ? "max-h-80" : "max-h-40"}`}>
        <button
          type="button"
          aria-pressed={selectedVisit === undefined}
          className={`${row} ${selectedVisit === undefined ? active : "bg-transparent hover:bg-hover"}`}
          onClick={() => onSelectVisit(undefined)}
        >
          <span className="truncate text-ink">State machine</span>
          <span className="text-secondary" data-machine-state={instance.state}>
            {instance.state ?? "—"}
          </span>
        </button>
        {visits.map((visit) => {
          const current = visit.visit === instance.visit && !instance.finished;
          return (
            <button
              key={visit.visit}
              type="button"
              data-visit={visit.visit}
              aria-pressed={selectedVisit === visit.visit}
              aria-label={`Visit ${visit.visit}, ${visit.state}${current ? ", current" : ""}`}
              className={`${row} ${selectedVisit === visit.visit ? active : "bg-transparent hover:bg-hover"}`}
              onClick={() => onSelectVisit(visit.visit)}
            >
              <span className="min-w-0 truncate">
                <span className="text-secondary">#{visit.visit}</span>{" "}
                <span className="text-ink">{visit.state}</span>
              </span>
              <span className="shrink-0 text-secondary">
                {current
                  ? "current"
                  : visit.left
                    ? `→ ${visit.left.to ?? "closed"} · ${visit.left.event ?? visit.left.source}`
                    : ""}
              </span>
            </button>
          );
        })}
        {history.error && (
          <p role="alert" className="px-[16px] py-1 text-[10px] text-danger">
            {history.error}{" "}
            <button type="button" className="underline" onClick={history.refresh}>
              Retry read
            </button>
          </p>
        )}
        {(history.cursor !== undefined || history.value?.next_cursor != null) && (
          <div className="flex gap-3 px-[16px] py-1.5 text-[10px]">
            {history.cursor !== undefined && (
              <button
                type="button"
                className="underline"
                onClick={() => history.setCursor(undefined)}
              >
                Latest visits
              </button>
            )}
            {history.value?.next_cursor != null && (
              <button
                type="button"
                className="underline"
                onClick={() => history.setCursor(history.value!.next_cursor!)}
              >
                Older visits
              </button>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

const runAction =
  "cursor-pointer rounded border border-line bg-panel px-[11px] py-[7px] text-[10px] text-ink hover:bg-hover disabled:cursor-wait disabled:opacity-50";

/**
 * Machine-only run actions, placed beside the normal run controls (Cancel): send
 * an event (opens its form in the side panel), retry an interrupted visit, and the
 * development timer trigger. Each appears only when the host implements it.
 */
export function MachineRunActions({
  api,
  runId,
  instance,
  enabled,
  onSend,
  onChanged,
}: {
  api: OperatorApi;
  runId: string;
  instance: MachineInstance | undefined;
  enabled: boolean;
  onSend: () => void;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const scope = useRef(true);
  useEffect(() => {
    scope.current = true;
    return () => {
      scope.current = false;
    };
  }, []);
  if (!instance || !enabled || instance.finished) return null;
  const retryable = ["interrupted", "needs_attention", "indeterminate"].includes(
    instance.status,
  );
  async function act(mutate: () => Promise<void>) {
    setBusy(true);
    setError(undefined);
    try {
      await mutate();
      if (scope.current) onChanged();
    } catch (cause) {
      if (scope.current) setError(cause instanceof Error ? cause.message : "Action failed");
    } finally {
      if (scope.current) setBusy(false);
    }
  }
  const actions = [
    api.sendMachineEvent && (
      <button key="send" type="button" className={runAction} onClick={onSend}>
        Send event
      </button>
    ),
    api.retryMachineVisit && retryable && (
      <button
        key="retry"
        type="button"
        className={runAction}
        disabled={busy}
        onClick={() => void act(() => api.retryMachineVisit!(runId))}
      >
        Retry visit
      </button>
    ),
    api.fireNextMachineTimer && instance.due !== null && (
      <button
        key="fire"
        type="button"
        className={runAction}
        disabled={busy}
        title="Development hosts only"
        onClick={() => void act(() => api.fireNextMachineTimer!(runId))}
      >
        Fire next timer
      </button>
    ),
  ].filter(Boolean);
  if (actions.length === 0) return null;
  return (
    <div role="group" aria-label="Machine actions" className="flex items-center gap-2">
      {actions}
      {error && (
        <p role="alert" className="m-0 max-w-48 text-[10px] text-danger">
          {error}
        </p>
      )}
    </div>
  );
}

/**
 * The send form in the run's side panel, the way the run input form opens there.
 * A failed send keeps its idempotency key when retried with the same payload.
 */
export function MachineSendPanel({
  api,
  runId,
  workflow,
  spec: catalogSpec,
  instance,
  onClose,
  onSent,
}: {
  api: OperatorApi;
  runId: string;
  /** The machine's catalog entry; payload files stage against it like run input files. */
  workflow: FlowInfoMsg;
  spec: MachineSpec;
  instance: MachineInstance | undefined;
  onClose: () => void;
  onSent: () => void;
}) {
  const spec = instance?.machine_spec ?? catalogSpec;
  const [event, setEvent] = useState<string>();
  const [receipt, setReceipt] = useState<MachineEventReceipt>();
  const idempotency = useRef<{ signature: string; key: string } | undefined>(undefined);
  const scope = useRef(true);
  useEffect(() => {
    scope.current = true;
    return () => {
      scope.current = false;
    };
  }, []);
  const events = new Map<string, MachineSpec["states"][string]["transitions"][number]>();
  for (const state of Object.values(spec.states))
    for (const transition of state.transitions)
      if (!events.has(transition.event)) events.set(transition.event, transition);
  // Event names may recur with state-specific payload schemas.
  for (const transition of (instance?.state && spec.states[instance.state]?.transitions) || [])
    events.set(transition.event, transition);
  const chosen = event && events.has(event) ? event : events.keys().next().value;
  return (
    <aside
      aria-label="Send event"
      className="inspector inspector-run relative flex h-full min-h-0 w-full min-w-0 flex-col overflow-hidden border-l border-line bg-panel"
    >
      <header className="flex shrink-0 items-start justify-between gap-3 border-b border-line px-5 pt-[19px] pb-3.5">
        <div className="min-w-0">
          <span className="eyebrow block font-mono text-[9px] tracking-[.16em] text-acid uppercase">
            Send event · Run <span className="normal-case">{runId}</span>
          </span>
          <h2 className="m-0 mt-1 text-lg">{instance ? runKeyLabel(instance.key) : runId}</h2>
        </div>
        <button
          type="button"
          aria-label="Close"
          className="cursor-pointer rounded border border-line bg-panel px-2 py-1 text-xs"
          onClick={onClose}
        >
          ×
        </button>
      </header>
      <div className="grid min-h-0 flex-1 content-start gap-4 overflow-auto px-5 py-4 text-xs">
        {chosen ? (
          <>
            <label className="grid gap-1 text-secondary">
              Event
              <select
                aria-label="Event"
                className="rounded border border-line bg-canvas p-2 text-ink"
                value={chosen}
                onChange={(e) => setEvent(e.target.value)}
              >
                {[...events.keys()].map((name) => (
                  <option key={name}>{name}</option>
                ))}
              </select>
            </label>
            <p className="m-0 text-secondary">
              Accepts now: {instance?.accepts.join(", ") || "nothing"}. Other events may be
              deferred or ignored by the host.
            </p>
            <SendEventForm
              key={`${chosen}:${JSON.stringify(events.get(chosen)!.payload_schema)}`}
              api={api}
              workflow={workflow}
              event={chosen}
              schema={events.get(chosen)!.payload_schema}
              onSend={async (payload, inputFiles) => {
                const signature = JSON.stringify([
                  chosen,
                  payload,
                  (inputFiles ?? []).map((file) => [file.fieldName, file.name, file.sha256]),
                ]);
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
                  inputFiles,
                );
                if (!scope.current) return;
                setReceipt(result);
                idempotency.current = undefined;
                onSent();
              }}
            />
            {receipt && (
              <p
                role="status"
                className="m-0 font-mono text-[11px]"
                data-receipt-status={receipt.status}
                data-receipt-duplicate={receipt.duplicate}
              >
                Receipt {receipt.receipt_id} · {receipt.key} · {receipt.status}
                {receipt.duplicate ? " · duplicate" : ""}
              </p>
            )}
          </>
        ) : (
          <p className="m-0 text-secondary">This machine declares no events.</p>
        )}
      </div>
    </aside>
  );
}

/**
 * One event's payload form: the run input form's fields over a payload draft, so
 * File fields pick and upload exactly like run inputs. The host validates the
 * complete payload schema.
 */
function SendEventForm({
  api,
  workflow,
  event,
  schema,
  onSend,
}: {
  api: OperatorApi;
  workflow: FlowInfoMsg;
  event: string;
  schema: JsonSchema | null;
  onSend: (payload: JsonValue, inputFiles?: InputFileAttachment[]) => Promise<void>;
}) {
  const draft = useInputDraft(workflow, api, event);
  const fields = useMemo(
    () =>
      schema
        ? workflowInputFields({ typeName: event, jsonSchema: schema, consumerNodeIds: [] })
        : [],
    [event, schema],
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  async function submit(formEvent: FormEvent) {
    formEvent.preventDefault();
    setError(undefined);
    const invalid = Object.keys(draft.pending);
    if (invalid.length > 0) {
      setError(`Fix the payload before sending: ${invalid.join(", ")} is not valid JSON`);
      return;
    }
    let payload: JsonValue = null;
    let inputFiles: InputFileAttachment[] | undefined;
    try {
      if (schema !== null) {
        let values = parseRunInput(draft.text);
        if (Object.keys(draft.files).length > 0)
          ({ input: values, inputFiles } = applyInputFiles(values, draft.files));
        payload = parseJsonValue(values, "payload");
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The payload is not valid.");
      return;
    }
    setBusy(true);
    try {
      await onSend(payload, inputFiles);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The request failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="grid gap-3" onSubmit={(e) => void submit(e)} aria-label="Send event">
      <fieldset disabled={busy} className="m-0 grid min-w-0 gap-3 border-0 p-0">
        {fields.length > 0 && <InputFieldList fields={fields} draft={draft} />}
        <button className={machineButton} type="submit">
          {busy ? "Sending…" : "Send event"}
        </button>
      </fieldset>
      {error && (
        <p role="alert" className="m-0 text-xs text-danger [overflow-wrap:anywhere]">
          {error}
        </p>
      )}
    </form>
  );
}

function WaitRow({ children, ...props }: React.ComponentProps<"li">) {
  return (
    <li
      className="grid gap-0.5 border-b border-line py-2 font-mono text-[11px] last:border-0"
      {...props}
    >
      {children}
    </li>
  );
}

/**
 * Machine rows for the run's waits panel (where a flow run shows its pending
 * waits): the pending timer, deferred events and accepted receipts.
 */
export function MachineRunWaits({
  instance,
  readError,
  refresh,
}: {
  instance: MachineInstance | undefined;
  readError?: string;
  refresh: () => void;
}) {
  if (!instance) return <ReadError error={readError} retry={refresh} />;
  const timerTarget =
    instance.state &&
    instance.machine_spec?.states[instance.state]?.timers.map((timer) => timer.target)[0];
  return (
    <section aria-label="Machine waits" className="p-3 text-xs">
      <ReadError error={readError} retry={refresh} />
      <ul className="m-0 list-none p-0">
        {instance.due !== null && (
          <WaitRow data-wait-kind="timer">
            <span className="text-ink">Timer{timerTarget ? ` → ${timerTarget}` : ""}</span>
            <Timer due={instance.due} />
          </WaitRow>
        )}
        {instance.pending_events === undefined ? (
          <WaitRow className="text-secondary">
            Pending events not reported by this host.
          </WaitRow>
        ) : (
          instance.pending_events.map((pending) => (
            <WaitRow key={pending.receipt_id} data-wait-kind="event">
              <span className="text-ink">Deferred · {pending.event}</span>
              <span className="text-secondary">
                {pending.receipt_id} · {JSON.stringify(pending.payload)}
              </span>
            </WaitRow>
          ))
        )}
        {(instance.receipts ?? []).map((item) => (
          <WaitRow key={item.receipt_id} data-receipt-status={item.status}>
            <span className="text-ink">
              {item.key} · {item.status}
            </span>
            <span className="text-secondary">
              {item.receipt_id} · {clock(item.accepted_at)}
            </span>
          </WaitRow>
        ))}
        {!instance.finished && instance.accepts.length > 0 && (
          <WaitRow data-wait-kind="accepts">
            <span className="text-ink">Waiting for event</span>
            <span className="text-secondary">{instance.accepts.join(", ")}</span>
          </WaitRow>
        )}
        {instance.due === null &&
          instance.pending_events?.length === 0 &&
          (instance.receipts ?? []).length === 0 &&
          (instance.finished || instance.accepts.length === 0) && (
            <WaitRow className="text-secondary" data-wait-kind="none">
              No pending waits.
            </WaitRow>
          )}
      </ul>
    </section>
  );
}

/**
 * A selected visit in the run's waits panel: its clocks, catalog revision and
 * transition, its waits, decision cards and terminal audits, and its event facts.
 */
export function MachineVisitWaits({
  api,
  runId,
  visit,
  readError,
  refresh,
  current,
  enabled,
}: {
  api: OperatorApi;
  runId: string;
  visit: MachineVisit | undefined;
  readError?: string;
  refresh: () => void;
  current: boolean;
  enabled: boolean;
}) {
  if (!visit)
    return (
      <div className="p-3 text-xs">
        <ReadError error={readError} retry={refresh} />
      </div>
    );
  return (
    <section aria-label={`Visit ${visit.visit}`} className="grid gap-3 p-3 text-xs">
      <ReadError error={readError} retry={refresh} />
      <dl className="m-0 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 font-mono text-[10px]">
        <dt className="text-secondary">Visit</dt>
        <dd className="m-0">
          #{visit.visit} · {visit.state} · {visit.disposition}
        </dd>
        <dt className="text-secondary">Entered</dt>
        <dd className="m-0">{clock(visit.entered_at)}</dd>
        <dt className="text-secondary">Left</dt>
        <dd className="m-0">{clock(visit.left_at)}</dd>
        <dt className="text-secondary">Revision</dt>
        <dd className="m-0">{visit.catalog_revision ?? "Not reported"}</dd>
        {visit.transition && (
          <>
            <dt className="text-secondary">Transition</dt>
            <dd className="m-0">
              {visit.transition.from} → {visit.transition.to ?? "closed"} ·{" "}
              {visit.transition.event ?? visit.transition.source}
            </dd>
          </>
        )}
      </dl>
      {visit.disposition === "expired" ? (
        <p className="m-0 text-secondary" data-run-history="expired">
          This visit&apos;s waits, event facts and steps expired under the run&apos;s retention
          policy.
        </p>
      ) : (
        <>
          {api.listRunWaits && (
            <RunWaitPanel
              key={`${runId}:${visit.visit}`}
              api={api}
              runId={runId}
              visit={visit.visit}
              readOnly={!enabled || !current}
              live={current}
            />
          )}
          <EventFacts api={api} runId={runId} visit={visit.visit} live={current} />
        </>
      )}
    </section>
  );
}
