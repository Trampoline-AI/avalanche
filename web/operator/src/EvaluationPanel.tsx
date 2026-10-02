import { AnswerSummary } from "./ClassifierDetails";
import type { EvaluationDeclaration } from "./classifier";
import { MetricInputSummary } from "./MetricInputSummary";
import type { EvaluationRecord } from "./evaluations";
import { ValueView } from "./ValueView";
import { Percentage } from "./Percentage";

interface EvaluationPanelProps {
  record?: EvaluationRecord;
  loading: boolean;
  error?: string;
  declaration?: EvaluationDeclaration;
}

export function EvaluationPanel({ record, loading, error, declaration }: EvaluationPanelProps) {
  return (
    <section aria-label="Execution evaluations" className="grid min-w-0 gap-5">
      {error && (
        <p role="alert" className="m-0 text-xs text-danger [overflow-wrap:anywhere]">
          {error}
        </p>
      )}
      {loading && (
        <p role="status" className="m-0 text-xs text-muted">
          Loading evaluation…
        </p>
      )}
      {!loading && !record && !error && (
        <p className="m-0 text-xs text-muted">No evaluation recorded for this execution.</p>
      )}
      {record && (
        <div className="flex flex-wrap items-baseline justify-between gap-2 text-[10px]">
          <span
            className={`font-mono uppercase ${record.status === "failed" ? "text-danger" : "text-secondary"}`}
          >
            {record.status}
          </span>
          {record.status === "completed" && (
            <span className="text-muted [overflow-wrap:anywhere]">
              {record.result.classification.model}
            </span>
          )}
        </div>
      )}
      {record?.status === "pending" && (
        <p role="status" className="m-0 text-xs text-muted">
          Evaluation in progress…
        </p>
      )}
      {record?.status === "failed" && (
        <p role="alert" className="m-0 text-xs text-danger [overflow-wrap:anywhere]">
          {record.error}
        </p>
      )}
      {record?.status === "completed" && (
        <>
          {Object.keys(record.result.composites).length > 0 && (
            <section
              aria-label="Composites"
              className="grid min-w-0 gap-5 border-t border-line pt-5"
            >
              <h3 className="m-0 font-mono text-[11px] font-semibold tracking-wider text-secondary uppercase">
                Composites
              </h3>
              <dl className="m-0 grid grid-cols-[minmax(0,1fr)_auto] gap-2 text-xs">
                {Object.entries(record.result.composites).map(([name, value]) => (
                  <div key={name} className="contents">
                    <dt className="[overflow-wrap:anywhere]">{name}</dt>
                    <dd className="m-0 font-semibold tabular-nums">
                      <Percentage value={value} decimal gradient />
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
          )}
          <section
            aria-label="Metrics"
            className="grid min-w-0 gap-5 border-t border-line pt-5"
          >
            <h3 className="m-0 font-mono text-[11px] font-semibold tracking-wider text-secondary uppercase">
              Metrics
            </h3>
            {Object.entries(record.result.classification.answers).map(([name, answer]) => (
              <section
                key={name}
                aria-label={`Metric ${name}`}
                className="grid min-w-0 gap-2 [&+section]:border-t [&+section]:border-line [&+section]:pt-4"
              >
                <div className="flex items-baseline justify-between gap-2">
                  <h4 className="m-0 text-xs font-semibold [overflow-wrap:anywhere]">{name}</h4>
                  <span className="font-mono text-[9px] text-muted">{answer.type}</span>
                </div>
                <MetricInputSummary inputs={declaration?.metric_inputs?.[name]} />
                <AnswerSummary id={name} answer={answer} expanded />
                {answer.type === "score" && (
                  <ol
                    start={0}
                    aria-label={`${name} level probabilities`}
                    className="m-0 grid list-none gap-2 pt-2 pl-0"
                  >
                    {Object.entries(answer.legend).map(([level, description]) => (
                      <li
                        key={level}
                        className="grid min-w-0 grid-cols-[1fr_auto] gap-1 text-[11px] text-secondary"
                      >
                        <span>Level {level}</span>
                        <Percentage value={answer.probabilities[level]} />
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
            ))}
          </section>
        </>
      )}
    </section>
  );
}
