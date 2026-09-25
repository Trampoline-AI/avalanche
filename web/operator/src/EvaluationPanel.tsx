import { useEffect, useState } from "react";

import type { OperatorApi } from "./api";
import { AnswerSummary } from "./ClassifierDetails";
import type { EvaluationRecord } from "./evaluations";
import { ValueView } from "./ValueView";

interface EvaluationPanelProps {
  api: OperatorApi;
  operatorInstanceId: string;
  runId: string;
  nodeId: string;
}

interface EvaluationState {
  api: OperatorApi;
  scope: string;
  records?: EvaluationRecord[];
  error?: string;
}

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 0 });
const scoreNumber = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 6 });
const POLL_INTERVAL_MS = 1500;

export function EvaluationPanel({
  api,
  operatorInstanceId,
  runId,
  nodeId,
}: EvaluationPanelProps) {
  const scope = `${operatorInstanceId}\0${runId}\0${nodeId}`;
  const [state, setState] = useState<EvaluationState>();
  const current = state?.api === api && state.scope === scope ? state : undefined;

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    async function refresh() {
      try {
        const records = await api.listEvaluations(runId, nodeId, controller.signal);
        if (!controller.signal.aborted) setState({ api, scope, records });
      } catch (error: unknown) {
        if (!controller.signal.aborted) {
          setState((previous) => ({
            api,
            scope,
            records:
              previous?.api === api && previous.scope === scope ? previous.records : undefined,
            error: error instanceof Error ? error.message : "Unable to load evaluations",
          }));
        }
      } finally {
        // Evaluation work can finish after the workflow's terminal structural snapshot.
        if (!controller.signal.aborted)
          timer = window.setTimeout(() => void refresh(), POLL_INTERVAL_MS);
      }
    }
    void refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [api, scope, runId, nodeId]);

  return (
    <section aria-label="Execution evaluations" className="grid min-w-0 gap-4">
      <div>
        <h3 className="inspector-section-title">Evaluations</h3>
        <p className="m-0 text-[11px] leading-relaxed text-muted">
          Post-execution judgments, separate from workflow status. Results may arrive after the
          run finishes.
        </p>
      </div>
      {current?.error && (
        <p role="alert" className="m-0 text-xs text-danger [overflow-wrap:anywhere]">
          {current.error}
        </p>
      )}
      {!current && (
        <p role="status" className="m-0 text-xs text-muted">
          Loading evaluations…
        </p>
      )}
      {current?.records?.length === 0 && (
        <p className="m-0 text-xs text-muted">No evaluations recorded for this execution.</p>
      )}
      {current?.records?.map((record, index) => (
        <article
          key={record.evaluationId}
          aria-label={`Evaluation ${index + 1}`}
          className="min-w-0 overflow-hidden rounded-md border border-line"
        >
          <header className="flex items-baseline justify-between gap-3 border-b border-line bg-panel px-3 py-2">
            <h4 className="m-0 text-xs font-semibold">Evaluation {index + 1}</h4>
            <span
              className={`font-mono text-[10px] uppercase ${record.status === "failed" ? "text-danger" : "text-secondary"}`}
            >
              {record.status}
            </span>
          </header>
          <div className="grid min-w-0 gap-4 p-3">
            {record.status === "pending" && (
              <p role="status" className="m-0 text-xs text-muted">
                Evaluation in progress…
              </p>
            )}
            {record.status === "failed" && (
              <p role="alert" className="m-0 text-xs text-danger [overflow-wrap:anywhere]">
                {record.error}
              </p>
            )}
            {record.status === "completed" && (
              <>
                <p className="m-0 text-[10px] text-muted [overflow-wrap:anywhere]">
                  Model: {record.result.classification.model}
                </p>
                <section aria-label="Metrics" className="grid min-w-0 gap-3">
                  <h5 className="m-0 font-mono text-[10px] text-muted uppercase">Metrics</h5>
                  {Object.entries(record.result.classification.answers).map(
                    ([name, answer]) => (
                      <section
                        key={name}
                        aria-label={`Metric ${name}`}
                        className="grid min-w-0 gap-2 rounded border border-line p-3"
                      >
                        <div className="flex items-baseline justify-between gap-2">
                          <h6 className="m-0 text-xs font-semibold [overflow-wrap:anywhere]">
                            {name}
                          </h6>
                          <span className="font-mono text-[9px] text-muted">{answer.type}</span>
                        </div>
                        {answer.type === "choice" && (
                          <p className="m-0 text-xs text-classifier [overflow-wrap:anywhere]">
                            Choice: {answer.choice}
                          </p>
                        )}
                        <AnswerSummary id={name} answer={answer} expanded />
                        {answer.type === "score" && (
                          <ol
                            start={0}
                            aria-label={`${name} level probabilities`}
                            className="m-0 grid list-none gap-2 border-t border-line pt-2 pl-0"
                          >
                            {Object.entries(answer.legend).map(([level, description]) => (
                              <li
                                key={level}
                                className="grid min-w-0 grid-cols-[1fr_auto] gap-1 text-[11px] text-secondary"
                              >
                                <span>Level {level}</span>
                                <span className="tabular-nums">
                                  {percent.format(answer.probabilities[level])}
                                </span>
                                <div className="col-span-2 min-w-0 [overflow-wrap:anywhere]">
                                  {typeof description === "string" ? (
                                    description
                                  ) : (
                                    <ValueView value={description} jsonOnly />
                                  )}
                                </div>
                              </li>
                            ))}
                          </ol>
                        )}
                      </section>
                    ),
                  )}
                </section>
                {Object.keys(record.result.composites).length > 0 && (
                  <section
                    aria-label="Composites"
                    className="grid gap-2 border-t border-line pt-3"
                  >
                    <h5 className="m-0 font-mono text-[10px] text-muted uppercase">
                      Composites · 0–1
                    </h5>
                    <dl className="m-0 grid grid-cols-[minmax(0,1fr)_auto] gap-2 text-xs">
                      {Object.entries(record.result.composites).map(([name, value]) => (
                        <div key={name} className="contents">
                          <dt className="[overflow-wrap:anywhere]">{name}</dt>
                          <dd className="m-0 font-semibold text-acid tabular-nums">
                            {scoreNumber.format(value)}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  </section>
                )}
              </>
            )}
          </div>
        </article>
      ))}
    </section>
  );
}
