import { useMemo } from "react";

import { ClassifierQuestions } from "./ClassifierDetails";
import { decodeEvaluationDeclaration } from "./classifier";

export function EvaluationDefinition({ raw }: { raw: string | undefined }) {
  const parsed = useMemo(() => {
    if (raw === undefined) return undefined;
    try {
      return { declaration: decodeEvaluationDeclaration(raw) };
    } catch (error: unknown) {
      return {
        error: error instanceof Error ? error.message : "Malformed evaluation declaration",
      };
    }
  }, [raw]);
  if (!parsed) return null;
  const declaration = parsed.declaration;
  return (
    <section aria-label="Evaluation declaration" className="min-w-0">
      <h3 className="inspector-section-title">Evaluations</h3>
      <p className="mt-0 mb-3 text-[11px] leading-relaxed text-muted">
        Operator-only, nonblocking evaluations run after the agent succeeds. They do not gate
        workflow execution or change its result. This is configuration, not a run result.
      </p>
      {declaration ? (
        <>
          <dl className="m-0 mb-3 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-[11px]">
            <dt className="text-muted">Model</dt>
            <dd className="m-0 font-mono [overflow-wrap:anywhere]">
              {declaration.runtime.model}
            </dd>
            <dt className="text-muted">Timeout</dt>
            <dd className="m-0">{declaration.runtime.timeout}s</dd>
          </dl>
          <h4 className="inspector-section-title">Metrics</h4>
          <ClassifierQuestions declaration={{ questions: declaration.metrics }} />
          {declaration.composites.length > 0 && (
            <section aria-label="Composite evaluations" className="mt-3">
              <h4 className="inspector-section-title">Composites</h4>
              <ul className="m-0 grid list-none gap-1 p-0 font-mono text-[11px]">
                {declaration.composites.map((name) => (
                  <li key={name} className="[overflow-wrap:anywhere]">
                    {name}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      ) : (
        <p role="alert" className="text-[11px] text-muted">
          Evaluation declaration unavailable: {parsed.error}
        </p>
      )}
    </section>
  );
}
