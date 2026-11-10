import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import type { OperatorApi } from "./api";
import type {
  MachineEventReceipt,
  MachineInstance,
  MachinePage,
  MachineSpec,
  MachineTransition,
} from "./machine";
import type { FlowInfoMsg, InputFileAttachment } from "./model";
import { parseRunInput } from "./RunControls";
import { runKeyLabel } from "./runKey";
import type { MachineVisitDetail } from "./timelineEntries";
import { parseJsonValue, type JsonSchema, type JsonValue } from "./stepInterface";
import { applyInputFiles, useInputDraft } from "./useInputDraft";
import { InputFieldList } from "./WorkflowInputInspector";
import { workflowInputFields } from "./workflowInput";

const machineButton =
  "rounded border border-line bg-panel px-3 py-2 text-xs text-ink disabled:opacity-50";

/**
 * When a machine read refreshes. `refreshKey` is the host's change signal for the
 * run (its summary revision from the update stream): a new key re-reads at once.
 * While `live`, a backoff poll (1 s doubling to 30 s, reset by each new key) covers
 * hosts whose stream does not reach every machine change; a settled run never polls.
 */
export interface MachineReadRefresh {
  live?: boolean;
  refreshKey?: string;
}

const READ_POLL_FIRST_MS = 1000;
const READ_POLL_MAX_MS = 30_000;
/** Stream bursts (many revisions a second while a visit runs) coalesce to this spacing. */
const READ_MIN_SPACING_MS = 250;

/**
 * One read at a time per `load`. Only a new `load` (or unmount) aborts a read in
 * flight. A new key (refreshKey, `refresh()`, liveness) during a read marks it
 * dirty, and one follow-up read starts READ_MIN_SPACING_MS after the read ends, so
 * a burst of revisions faster than the host answers still completes reads and ends
 * with a read of the latest state.
 */
function useRead<T>(
  load: ((signal: AbortSignal) => Promise<T>) | undefined,
  { live = false, refreshKey }: MachineReadRefresh = {},
) {
  const [result, setResult] = useState<{ load: typeof load; value: T }>();
  const [failure, setFailure] = useState<{ load: typeof load; error: string }>();
  const value = result?.load === load ? result?.value : undefined;
  const error = failure?.load === load ? failure?.error : undefined;
  const [revision, refresh] = useState(0);
  const liveRef = useRef(live);
  const reader = useRef<{ request: () => void }>(undefined);
  useEffect(() => {
    liveRef.current = live;
  }, [live]);
  // Declared before the reader so a key change that comes with a new load reaches
  // no reader (the old one is cleaned up; the new one reads at once).
  useEffect(() => {
    reader.current?.request();
  }, [live, refreshKey, revision]);
  useEffect(() => {
    if (!load) return;
    const readValue = load;
    const controller = new AbortController();
    let timeout: ReturnType<typeof setTimeout> | undefined;
    let reading = false;
    let dirty = false;
    let endedAt = -Infinity;
    let delay = READ_POLL_FIRST_MS;
    const schedule = (ms: number) => {
      clearTimeout(timeout);
      timeout = setTimeout(() => void read(), ms);
    };
    async function read() {
      clearTimeout(timeout);
      reading = true;
      dirty = false;
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
        reading = false;
        endedAt = Date.now();
        if (!controller.signal.aborted) {
          if (dirty) schedule(READ_MIN_SPACING_MS);
          else if (liveRef.current) {
            schedule(delay);
            delay = Math.min(delay * 2, READ_POLL_MAX_MS);
          }
        }
      }
    }
    reader.current = {
      request() {
        delay = READ_POLL_FIRST_MS;
        if (reading) dirty = true;
        else schedule(Math.max(0, READ_MIN_SPACING_MS - (Date.now() - endedAt)));
      },
    };
    void read();
    return () => {
      controller.abort();
      clearTimeout(timeout);
      reader.current = undefined;
    };
  }, [load]);
  return { value, error, refresh: () => refresh((current) => current + 1) };
}

/** The run's machine instance, re-read on each run change (see MachineReadRefresh). */
export function useMachineInstance(
  api: OperatorApi,
  runId: string | undefined,
  refresh: MachineReadRefresh = {},
) {
  const load = useCallback(
    (signal: AbortSignal) => api.getMachineInstance!(runId!, signal),
    [api, runId],
  );
  return useRead(api.getMachineInstance && runId ? load : undefined, refresh);
}

/** The newest window of the run's transitions; follows the run while it is the first window. */
export function useMachineHistory(
  api: OperatorApi,
  runId: string | undefined,
  { live, refreshKey }: MachineReadRefresh = {},
) {
  const [cursor, setCursor] = useState<{ runId?: string; value?: string }>();
  const current = cursor && cursor.runId === runId ? cursor.value : undefined;
  const load = useCallback(
    (signal: AbortSignal): Promise<MachinePage<MachineTransition>> =>
      api.getMachineHistory!(runId!, { cursor: current, limit: 25 }, signal),
    [api, runId, current],
  );
  const first = current === undefined;
  const read = useRead(api.getMachineHistory && runId ? load : undefined, {
    live: live && first,
    refreshKey: first ? refreshKey : undefined,
  });
  return {
    ...read,
    cursor: current,
    setCursor: (value: string | undefined) => setCursor({ runId, value }),
  };
}
export type MachineHistory = ReturnType<typeof useMachineHistory>;

/** One visit; follows the run while it is the instance's current visit. */
export function useMachineVisit(
  api: OperatorApi,
  runId: string | undefined,
  visit: number | undefined,
  live: boolean,
  refreshKey: string,
) {
  const load = useCallback(
    (signal: AbortSignal) => api.getMachineVisit!(runId!, visit!, signal),
    [api, runId, visit],
  );
  return useRead(api.getMachineVisit && runId && visit !== undefined ? load : undefined, {
    live,
    refreshKey,
  });
}

/**
 * The run's waits (decision, event and sleep cards), read on each run change like
 * the machine reads (no fixed poll); absent when the host does not list waits.
 */
export function useRunWaits(
  api: OperatorApi,
  runId: string | undefined,
  refresh: MachineReadRefresh = {},
) {
  const load = useCallback(() => api.listRunWaits!(runId!), [api, runId]);
  return useRead(api.listRunWaits && runId ? load : undefined, refresh);
}

const EMPTY_VISIT_DETAILS: ReadonlyMap<number, MachineVisitDetail> = new Map();
/** Event facts read per visit for the timeline; later pages load on request. */
export const VISIT_EVENTS_PAGE = 50;

/**
 * The visit records and first event-fact pages of the visits the timeline shows.
 * A visit the run has left never changes, so it is read once; only the live visit
 * re-reads, when `refreshKey` (the run's change signal) moves. One visit is read at
 * a time, and a key change never aborts a read in flight (it marks the live visit
 * stale, read again READ_MIN_SPACING_MS after the read ends), so a burst of
 * revisions still completes reads. No timer polls.
 */
export function useMachineVisitDetails(
  api: OperatorApi,
  runId: string | undefined,
  visits: readonly number[],
  liveVisit: number | undefined,
  refreshKey: string | undefined,
) {
  const [store, setStore] = useState<{
    runId?: string;
    details: ReadonlyMap<number, MachineVisitDetail>;
  }>({ details: EMPTY_VISIT_DETAILS });
  const details = store.runId === runId ? store.details : EMPTY_VISIT_DETAILS;
  const visitsKey = visits.join(",");
  const wanted = useRef({ visits: [] as number[], liveVisit, refreshKey });
  const loader = useRef<{ request: () => void; retry: () => void }>(undefined);
  const readable = Boolean(api.getMachineVisit || api.getMachineVisitEvents);
  useLayoutEffect(() => {
    wanted.current = {
      visits: visitsKey ? visitsKey.split(",").map(Number) : [],
      liveVisit,
      refreshKey,
    };
    loader.current?.request();
  }, [visitsKey, liveVisit, refreshKey]);
  useEffect(() => {
    if (!runId || !readable) return;
    const controller = new AbortController();
    // visit -> the key it was read at ("" for a visit the run has left).
    const read = new Map<number, string>();
    let reading = false;
    let endedAt = -Infinity;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const spaced = () => Math.max(0, READ_MIN_SPACING_MS - (Date.now() - endedAt));
    const keyFor = (visit: number) =>
      visit === wanted.current.liveVisit ? (wanted.current.refreshKey ?? "") : "";
    const next = () => {
      const { visits: shown, liveVisit: live } = wanted.current;
      // The live visit first, then newest to oldest.
      const order = [...shown].sort((left, right) =>
        left === live ? -1 : right === live ? 1 : right - left,
      );
      return order.find((visit) => read.get(visit) !== keyFor(visit));
    };
    const schedule = (ms: number) => {
      clearTimeout(timeout);
      timeout = setTimeout(() => void step(), ms);
    };
    async function step() {
      const visit = next();
      if (visit === undefined || controller.signal.aborted) return;
      reading = true;
      const key = keyFor(visit);
      read.set(visit, key);
      const detail: MachineVisitDetail = {};
      try {
        const [record, events] = await Promise.all([
          api.getMachineVisit?.(runId!, visit, controller.signal),
          api.getMachineVisitEvents?.(
            runId!,
            visit,
            { limit: VISIT_EVENTS_PAGE },
            controller.signal,
          ),
        ]);
        if (record) detail.record = record;
        if (events) {
          detail.events = events.items;
          detail.eventsCursor = events.next_cursor;
        }
      } catch (cause) {
        detail.error = cause instanceof Error ? cause.message : "Read failed";
      } finally {
        reading = false;
        endedAt = Date.now();
      }
      if (controller.signal.aborted) return;
      setStore((current) => {
        const map = new Map(current.runId === runId ? current.details : []);
        map.set(visit, detail);
        return { runId, details: map };
      });
      // A live visit whose key moved during the read is read again, spaced.
      // Unchanged visits still to read go at once; a re-read waits for the spacing.
      const following = next();
      if (following !== undefined) schedule(read.has(following) ? spaced() : 0);
    }
    loader.current = {
      request() {
        if (reading) return;
        const following = next();
        if (following !== undefined) schedule(read.has(following) ? spaced() : 0);
      },
      retry() {
        for (const [visit] of read) read.delete(visit);
        if (!reading) schedule(0);
      },
    };
    schedule(0);
    return () => {
      controller.abort();
      clearTimeout(timeout);
      loader.current = undefined;
    };
  }, [api, runId, readable]);
  const loadMoreEvents = useCallback(
    async (visit: number) => {
      const cursor = details.get(visit)?.eventsCursor;
      if (!runId || !cursor || !api.getMachineVisitEvents) return;
      try {
        const page = await api.getMachineVisitEvents(runId, visit, {
          cursor,
          limit: VISIT_EVENTS_PAGE,
        });
        setStore((current) => {
          if (current.runId !== runId) return current;
          const previous = current.details.get(visit) ?? {};
          const map = new Map(current.details);
          map.set(visit, {
            ...previous,
            events: [...(previous.events ?? []), ...page.items],
            eventsCursor: page.next_cursor,
          });
          return { runId, details: map };
        });
      } catch (cause) {
        setStore((current) => {
          if (current.runId !== runId) return current;
          const map = new Map(current.details);
          map.set(visit, {
            ...current.details.get(visit),
            error: cause instanceof Error ? cause.message : "Read failed",
          });
          return { runId, details: map };
        });
      }
    },
    [api, details, runId],
  );
  return {
    details,
    loadMoreEvents,
    retry: useCallback(() => loader.current?.retry(), []),
  };
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

type MachineTransitionSpec = MachineSpec["states"][string]["transitions"][number];

/** An event the run can be sent now, with the payload schema its consumer declares. */
export interface SendableEvent {
  name: string;
  /** In the current state's `defer=` list: buffered for a later accepting state. */
  deferred: boolean;
  payload_schema: JsonSchema | null;
}

/**
 * The events a run can be sent now, per the machine contract: those its current
 * state accepts (`instance.accepts`, its transitions), then those it defers (the
 * state's `defer=` list), which the host buffers and acknowledges as accepted.
 * Any other name is ignored by the host, so it is never offered. A deferred name
 * takes its payload schema from its declared consumer edge (none: an empty body).
 */
export function sendableEvents(
  spec: MachineSpec | undefined,
  instance: MachineInstance | undefined,
): SendableEvent[] {
  if (!spec || !instance) return [];
  const declared = new Map<string, MachineTransitionSpec>();
  for (const state of Object.values(spec.states))
    for (const transition of state.transitions)
      if (!declared.has(transition.event)) declared.set(transition.event, transition);
  // Event names may recur with state-specific payload schemas.
  const current = instance.state ? spec.states[instance.state] : undefined;
  for (const transition of current?.transitions ?? [])
    declared.set(transition.event, transition);
  const events: SendableEvent[] = [];
  const seen = new Set<string>();
  const offer = (name: string, deferred: boolean) => {
    const transition = declared.get(name);
    // An accepted name missing from the spec in hand has no known payload schema.
    if (seen.has(name) || (!transition && !deferred)) return;
    seen.add(name);
    events.push({ name, deferred, payload_schema: transition?.payload_schema ?? null });
  };
  for (const name of instance.accepts) offer(name, false);
  if (!instance.finished) for (const name of current?.defer ?? []) offer(name, true);
  return events;
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
  spec,
  enabled,
  onSend,
  onChanged,
}: {
  api: OperatorApi;
  runId: string;
  instance: MachineInstance | undefined;
  /** The machine's catalog spec, used when the host's instance omits its own. */
  spec?: MachineSpec;
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
  const sendable = sendableEvents(instance.machine_spec ?? spec, instance).length > 0;
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
      <button
        key="send"
        type="button"
        className={runAction}
        disabled={!sendable}
        title={sendable ? undefined : "This run accepts no events now"}
        onClick={onSend}
      >
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
  const declaredEvents = Object.values(spec.states).some((state) => state.transitions.length);
  // Offer what the run accepts or defers now, revalidated on every read: after a
  // transition, a choice the new state neither accepts nor defers falls back to
  // the first offered event, and a run that can take nothing offers nothing.
  const offered = sendableEvents(spec, instance);
  const events = new Map(offered.map((item) => [item.name, item] as const));
  const chosen: string | undefined =
    event !== undefined && events.has(event) ? event : offered[0]?.name;
  const accepts = offered.filter((item) => !item.deferred).map((item) => item.name);
  const defers = offered.filter((item) => item.deferred).map((item) => item.name);
  // The latest offered events, read when a send starts (a read may land mid-form).
  const sendable = useRef<string[]>([]);
  sendable.current = offered.map((item) => item.name);
  const offeredKey = sendable.current.join("\0");
  useEffect(() => {
    setEvent((current) =>
      current !== undefined && offeredKey.split("\0").includes(current) ? current : undefined,
    );
  }, [offeredKey]);
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
      <div className="grid min-h-0 min-w-0 flex-1 grid-cols-[minmax(0,1fr)] content-start gap-4 overflow-auto px-5 py-4 text-xs">
        {!declaredEvents ? (
          <p className="m-0 text-secondary">This machine declares no events.</p>
        ) : !instance ? (
          // The run is still loading: what it accepts is not known yet.
          <p role="status" className="m-0 text-secondary" data-send-loading>
            Loading the events this run accepts…
          </p>
        ) : chosen === undefined ? (
          <>
            <p className="m-0 text-secondary">This run accepts no events now.</p>
            <form
              className="grid gap-3"
              aria-label="Send event"
              onSubmit={(formEvent) => formEvent.preventDefault()}
            >
              <button className={machineButton} type="submit" disabled>
                Send event
              </button>
            </form>
          </>
        ) : (
          <>
            <label className="grid gap-1 text-secondary">
              Event
              <select
                aria-label="Event"
                className="rounded border border-line bg-canvas p-2 text-ink"
                value={chosen}
                onChange={(e) => setEvent(e.target.value)}
              >
                {offered.map((item) => (
                  <option key={item.name} value={item.name}>
                    {item.deferred ? `${item.name} (deferred)` : item.name}
                  </option>
                ))}
              </select>
            </label>
            <p className="m-0 text-secondary">
              {accepts.length > 0 && `Accepts now: ${accepts.join(", ")}.`}
              {accepts.length > 0 && defers.length > 0 && " "}
              {defers.length > 0 &&
                `Defers: ${defers.join(", ")} (held until a later state accepts it).`}
            </p>
            <SendEventForm
              key={`${chosen}:${JSON.stringify(events.get(chosen)!.payload_schema)}`}
              api={api}
              workflow={workflow}
              event={chosen}
              schema={events.get(chosen)!.payload_schema}
              onSend={async (payload, inputFiles) => {
                if (!sendable.current.includes(chosen))
                  throw new Error(`This run no longer accepts ${chosen}.`);
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
          </>
        )}
        {/* The last receipt stays after the run moves on (or stops accepting events). */}
        {receipt && (
          <p
            role="status"
            className="m-0 min-w-0 font-mono text-[11px] [overflow-wrap:anywhere]"
            data-receipt-status={receipt.status}
            data-receipt-duplicate={receipt.duplicate}
          >
            Receipt {receipt.receipt_id} · {receipt.key} · {receipt.status}
            {receipt.duplicate ? " · duplicate" : ""}
          </p>
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
