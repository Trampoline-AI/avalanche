import { useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";

import type { OperatorApi } from "./api";
import { parseAgentDeclaration } from "./GraphCanvas";
import type {
  AgentEventDescriptorMsg,
  FlowInfoMsg,
  LogRecordDescriptorMsg,
  NodeSnapshotMsg,
  RunSnapshotMsg,
} from "./generated/operator";
import { isUnknownRecord } from "./guards";
import { ValueView } from "./ValueView";

interface InspectorProps {
  api: OperatorApi;
  workflow?: FlowInfoMsg;
  run?: RunSnapshotMsg;
  nodeId?: string;
  liveEvents?: AgentEventDescriptorMsg[];
  liveLogs?: LogRecordDescriptorMsg[];
  onClose: () => void;
}

type RunTab = "overview" | "inputs" | "output" | "trace" | "logs";

function JsonBlock({ value }: { value: unknown }) {
  return <pre className="json-block">{JSON.stringify(value, null, 2)}</pre>;
}

function eventPayload(value: unknown): Record<string, unknown> | undefined {
  if (!isUnknownRecord(value)) return undefined;
  return isUnknownRecord(value.data) ? value.data : value;
}

export function Inspector({
  api,
  workflow,
  run,
  nodeId,
  liveEvents = [],
  liveLogs = [],
  onClose,
}: InspectorProps) {
  const [tab, setTab] = useState<RunTab>("overview");
  const [events, setEvents] = useState<AgentEventDescriptorMsg[]>([]);
  const [logs, setLogs] = useState<LogRecordDescriptorMsg[]>([]);
  const [selectedEvent, setSelectedEvent] = useState<string>();
  const [detail, setDetail] = useState<unknown>();
  const [detailError, setDetailError] = useState<string>();
  const [following, setFollowing] = useState(true);
  const detailCache = useRef(new Map<string, unknown>());
  const scrollParent = useRef<HTMLDivElement>(null);
  const node: NodeSnapshotMsg | undefined = run?.nodes.find((item) => item.nodeId === nodeId);
  const declarationJson = run
    ? run.topology?.agentMetadataJson[nodeId ?? ""]
    : workflow?.agentMetadataJson[nodeId ?? ""];
  const declaration = declarationJson ? parseAgentDeclaration(declarationJson) : undefined;

  useEffect(() => {
    setTab("overview");
    setEvents([]);
    setLogs([]);
    setSelectedEvent(undefined);
    setDetail(undefined);
    setFollowing(true);
    detailCache.current.clear();
    if (!run || !nodeId) return;
    let active = true;
    void Promise.all([api.listAgentEvents(run, nodeId), api.listLogs(run)])
      .then(([nextEvents, nextLogs]) => {
        if (!active) return;
        setEvents(nextEvents);
        setLogs(nextLogs.filter((entry) => !entry.nodeId || entry.nodeId === nodeId));
      })
      .catch((error: unknown) => {
        if (active) {
          setDetailError(error instanceof Error ? error.message : "Details unavailable");
        }
      });
    return () => {
      active = false;
    };
  }, [api, nodeId, run]);

  const combinedEvents = useMemo(() => {
    const bySequence = new Map<string, AgentEventDescriptorMsg>();
    for (const event of [...events, ...liveEvents]) bySequence.set(event.eventSequence, event);
    return [...bySequence.values()].sort(
      (left, right) => Number(left.eventSequence) - Number(right.eventSequence),
    );
  }, [events, liveEvents]);
  const turns = combinedEvents.filter((event) => event.eventKind === "iteration.recorded");
  const combinedLogs = useMemo(() => {
    const bySequence = new Map<string, LogRecordDescriptorMsg>();
    for (const entry of [...logs, ...liveLogs]) bySequence.set(entry.sequence, entry);
    return [...bySequence.values()].sort(
      (left, right) => Number(left.sequence) - Number(right.sequence),
    );
  }, [liveLogs, logs]);
  const traceUsage = useMemo<unknown>(() => {
    const usageJson = node?.trace?.header?.usageJson;
    return usageJson ? JSON.parse(usageJson) : undefined;
  }, [node?.trace?.header?.usageJson]);
  const traceTelemetry = useMemo<unknown>(() => {
    const telemetryJson = node?.trace?.header?.telemetryJson;
    return telemetryJson ? JSON.parse(telemetryJson) : undefined;
  }, [node?.trace?.header?.telemetryJson]);
  const declaredFields =
    tab === "inputs" ? declaration?.inputs : tab === "output" ? declaration?.outputs : undefined;
  const virtualizer = useVirtualizer({
    count: turns.length,
    getScrollElement: () => scrollParent.current,
    estimateSize: () => 64,
    overscan: 6,
  });

  useEffect(() => {
    if (!following || !turns.length) return;
    setSelectedEvent(turns.at(-1)!.eventSequence);
  }, [following, turns]);

  useEffect(() => {
    const descriptor = combinedEvents.find((event) => event.eventSequence === selectedEvent);
    if (!descriptor?.bodyToken) {
      setDetail(undefined);
      return;
    }
    const cached = detailCache.current.get(descriptor.bodyToken);
    if (cached !== undefined) {
      setDetail(cached);
      return;
    }
    let active = true;
    setDetail(undefined);
    setDetailError(undefined);
    void api
      .readDetail(descriptor.bodyToken)
      .then((body) => {
        if (!active) return;
        detailCache.current.delete(descriptor.bodyToken);
        detailCache.current.set(descriptor.bodyToken, body);
        while (detailCache.current.size > 8) {
          const oldest = detailCache.current.keys().next().value;
          if (oldest === undefined) break;
          detailCache.current.delete(oldest);
        }
        setDetail(body);
      })
      .catch((error: unknown) => {
        if (active) {
          setDetailError(error instanceof Error ? error.message : "Detail unavailable");
        }
      });
    return () => {
      active = false;
    };
  }, [api, combinedEvents, selectedEvent]);

  useEffect(() => {
    const kind =
      tab === "inputs" ? "run.started" : tab === "output" ? "run.succeeded" : undefined;
    if (!kind) return;
    const descriptor = [...combinedEvents]
      .reverse()
      .find((event) => event.eventKind === kind);
    if (descriptor) setSelectedEvent(descriptor.eventSequence);
  }, [combinedEvents, tab]);

  if (!run && workflow && nodeId) {
    return (
      <aside className="inspector" aria-label="Node declaration">
        <header>
          <div>
            <span className="eyebrow">Declaration</span>
            <h2>{workflow.displayNames[nodeId] || nodeId}</h2>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        {declaration ? (
          <div className="inspector-body declaration">
            <section>
              <h3>Instructions</h3>
              <p className="instructions">{declaration.instructions || "No instructions"}</p>
            </section>
            <section className="signature-columns">
              <div>
                <h3>Inputs</h3>
                {declaration.inputs.map((field) => (
                  <div className="field-detail" key={field.name}>
                    <strong>{field.name}</strong>
                    <code>{field.type}</code>
                    <p>{field.description}</p>
                  </div>
                ))}
              </div>
              <div>
                <h3>Outputs</h3>
                {declaration.outputs.map((field) => (
                  <div className="field-detail" key={field.name}>
                    <strong>{field.name}</strong>
                    <code>{field.type}</code>
                    <p>{field.description}</p>
                  </div>
                ))}
              </div>
            </section>
            <section>
              <h3>Runtime</h3>
              <JsonBlock value={declaration.runtime} />
            </section>
            <section>
              <h3>Models</h3>
              <JsonBlock value={declaration.model} />
            </section>
            <section>
              <h3>Skills & tools</h3>
              <JsonBlock value={{ skills: declaration.skills, tools: declaration.tools }} />
            </section>
          </div>
        ) : (
          <p className="empty-copy">This node has no agent declaration metadata.</p>
        )}
      </aside>
    );
  }

  if (!run || !node) return null;
  const selectedPayload = eventPayload(detail);
  const valueKey = tab === "inputs" ? "inputs" : "outputs";

  return (
    <aside className="inspector" aria-label="Run inspector">
      <header>
        <div>
          <span className="eyebrow">Historical execution</span>
          <h2>{node.name}</h2>
          <span className={`status-pill status-${node.status}`}>{node.status}</span>
        </div>
        <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
          ×
        </button>
      </header>
      <nav className="inspector-tabs" aria-label="Run detail views">
        {(["overview", "inputs", "output", "trace", "logs"] as RunTab[]).map((item) => (
          <button
            type="button"
            key={item}
            className={tab === item ? "active" : ""}
            onClick={() => setTab(item)}
          >
            {item}
          </button>
        ))}
      </nav>
      <div className="inspector-body">
        {detailError && <p className="error-banner">{detailError}</p>}
        {tab === "overview" && (
          <>
            <section className="metric-grid">
              <div><small>Status</small><strong>{node.status}</strong></div>
              <div><small>Revision</small><strong>{node.revision}</strong></div>
              <div><small>Started</small><strong>{node.startedAt ? "yes" : "—"}</strong></div>
              <div>
                <small>Duration</small>
                <strong>
                  {node.startedAt && node.endedAt
                    ? `${Math.max(0, node.endedAt - node.startedAt).toFixed(2)}s`
                    : "—"}
                </strong>
              </div>
            </section>
            {node.error && <p className="node-failure">{node.error}</p>}
            {node.trace && (
              <section>
                <h3>Trace header</h3>
                <dl className="trace-header">
                  <div><dt>Status</dt><dd>{node.trace.status}</dd></div>
                  <div><dt>Events</dt><dd>{node.trace.eventCount}</dd></div>
                  <div><dt>Size</dt><dd>{node.trace.sizeBytes} B</dd></div>
                  <div><dt>Complete</dt><dd>{node.trace.complete ? "yes" : "no"}</dd></div>
                  {node.trace.header && (
                    <>
                      <div><dt>Model</dt><dd>{node.trace.header.model}</dd></div>
                      <div>
                        <dt>Iterations</dt>
                        <dd>
                          {node.trace.header.iterations}/{node.trace.header.maxIterations}
                        </dd>
                      </div>
                      <div><dt>Duration</dt><dd>{node.trace.header.durationMs} ms</dd></div>
                    </>
                  )}
                </dl>
                {traceUsage !== undefined && (
                  <div className="trace-summary">
                    <h3>Usage</h3>
                    <ValueView value={traceUsage} />
                  </div>
                )}
                {traceTelemetry !== undefined && (
                  <div className="trace-summary">
                    <h3>Telemetry</h3>
                    <ValueView value={traceTelemetry} />
                  </div>
                )}
              </section>
            )}
          </>
        )}
        {(tab === "inputs" || tab === "output") && (
          <section>
            <h3>{tab === "inputs" ? "Invocation inputs" : "Terminal output"}</h3>
            {declaredFields?.length ? (
              <div className="declared-fields">
                <small>Declared fields</small>
                {declaredFields.map((field) => (
                  <span key={field.name}>
                    <strong>{field.name}</strong>
                    <code>{field.type}</code>
                  </span>
                ))}
              </div>
            ) : null}
            {selectedPayload && valueKey in selectedPayload ? (
              <ValueView value={selectedPayload[valueKey]} />
            ) : (
              <p className="empty-copy">No retained {tab} are available.</p>
            )}
          </section>
        )}
        {tab === "trace" && (
          <section className="trace-layout">
            <div className="trace-toolbar">
              <div>
                <h3>RunTrace</h3>
                <span>{turns.length} complete turns</span>
              </div>
              <button
                type="button"
                className={following ? "toggle active" : "toggle"}
                onClick={() => setFollowing((value) => !value)}
              >
                {following ? "Following live" : "Follow latest"}
              </button>
            </div>
            <div className="turn-list" ref={scrollParent}>
              <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
                {virtualizer.getVirtualItems().map((row) => {
                  const event = turns[row.index];
                  return (
                    <button
                      type="button"
                      key={event.eventSequence}
                      className={`turn-row ${selectedEvent === event.eventSequence ? "active" : ""} ${event.error ? "failed" : ""}`}
                      style={{ transform: `translateY(${row.start}px)` }}
                      onClick={() => {
                        setFollowing(false);
                        setSelectedEvent(event.eventSequence);
                      }}
                    >
                      <strong>Turn {event.iteration ?? row.index + 1}</strong>
                      <span>{event.durationMs ? `${event.durationMs} ms` : "—"}</span>
                      <small>{event.toolCount} tools · {event.predictCount} predicts</small>
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="turn-detail">
              {detail !== undefined ? <ValueView value={detail} /> : <p className="empty-copy">Select a turn.</p>}
            </div>
          </section>
        )}
        {tab === "logs" && (
          <section>
            <h3>Node logs</h3>
            <div className="log-list">
              {combinedLogs.map((entry) => (
                <button
                  type="button"
                  key={entry.sequence}
                  onClick={() => {
                    void api.readDetail(entry.bodyToken).then(setDetail).catch((error: unknown) => {
                      setDetailError(error instanceof Error ? error.message : "Log unavailable");
                    });
                  }}
                >
                  <span className={`log-level level-${entry.level}`}>{entry.level}</span>
                  <time>{new Date(entry.timestamp * 1000).toLocaleTimeString()}</time>
                  <span>#{entry.sequence}</span>
                </button>
              ))}
            </div>
            {detail !== undefined && <ValueView value={detail} />}
          </section>
        )}
      </div>
    </aside>
  );
}
