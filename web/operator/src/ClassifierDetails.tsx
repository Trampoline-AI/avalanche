import type { ReactNode } from "react";
import { Check, ListFilter, ListOrdered, ToggleLeft } from "lucide-react";

import type {
  ClassifierAnswer,
  ClassifierDeclaration,
  ClassifierEntry,
  ClassifierInvocation,
  ClassifierQuestion,
} from "./classifier";
import { ValueView } from "./ValueView";

function DetailValue({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] text-muted">{label}</dt>
      <dd className="m-0 mt-1 min-w-0 text-[11px] [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}

function JsonValue({ value }: { value: ClassifierEntry }) {
  return (
    <div className="classifier-value min-w-0 rounded-md bg-canvas p-2 text-[11px]">
      <ValueView value={value} jsonOnly />
    </div>
  );
}

function QuestionKind({ kind }: { kind: ClassifierQuestion["type"] }) {
  const Icon = kind === "choice" ? ListFilter : kind === "noul" ? ToggleLeft : ListOrdered;
  return (
    <span className="inline-flex shrink-0 items-center gap-1 rounded bg-classifier-light px-1.5 py-1 text-[10px] font-medium text-classifier">
      <Icon className="size-3" aria-hidden="true" />
      {kind === "choice" ? "Choice" : kind === "noul" ? "Noul" : "Score"}
    </span>
  );
}

function QuestionFrame({
  id,
  question,
  children,
}: {
  id: string;
  question: ClassifierQuestion;
  children: ReactNode;
}) {
  return (
    <section
      aria-label={`Question ${id}`}
      className="field-detail grid min-w-0 gap-3 rounded-lg border border-line bg-panel p-3"
    >
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <h4 className="m-0 min-w-0 text-xs font-semibold [overflow-wrap:anywhere]">{id}</h4>
        <QuestionKind kind={question.type} />
      </div>
      <div className="grid min-w-0 gap-1.5" role="group" aria-label="Instructions">
        <h5 className="m-0 text-[10px] font-medium text-muted">Instructions</h5>
        <JsonValue value={question.instructions} />
      </div>
      {children}
    </section>
  );
}

function ChoiceQuestion({
  id,
  question,
}: {
  id: string;
  question: Extract<ClassifierQuestion, { type: "choice" }>;
}) {
  return (
    <QuestionFrame id={id} question={question}>
      <div className="grid min-w-0 gap-2">
        <p className="m-0 text-[10px] text-muted">Choose one named option</p>
        <ul className="m-0 grid min-w-0 list-none gap-2 p-0" aria-label={`${id} options`}>
          {Object.entries(question.criteria).map(([name, criterion]) => (
            <li
              key={name}
              className="grid min-w-0 gap-1.5 rounded-md border border-classifier/20 p-2"
            >
              <h5 className="m-0 text-[11px] font-semibold text-classifier [overflow-wrap:anywhere]">
                {name}
              </h5>
              <JsonValue value={criterion} />
            </li>
          ))}
        </ul>
      </div>
    </QuestionFrame>
  );
}

function NoulQuestion({
  id,
  question,
}: {
  id: string;
  question: Extract<ClassifierQuestion, { type: "noul" }>;
}) {
  return (
    <QuestionFrame id={id} question={question}>
      <p className="m-0 text-[10px] text-muted">
        Estimate the probability of yes, from 0 (no) to 1 (yes).
      </p>
      {question.criteria === null ? (
        <div className="grid min-w-0 gap-1">
          <span className="text-[10px] text-muted">No additional criteria</span>
          <JsonValue value={null} />
        </div>
      ) : (
        <div className="grid min-w-0 grid-cols-[repeat(auto-fit,minmax(min(100%,8rem),1fr))] gap-2">
          <div
            role="group"
            aria-label={`${id} yes criterion`}
            className="grid min-w-0 content-start gap-1.5 rounded-md border border-classifier/25 bg-classifier-light/50 p-2"
          >
            <h5 className="m-0 text-[11px] font-semibold text-classifier">
              Yes <span className="font-mono font-normal">(true)</span>
            </h5>
            {question.criteria.true === undefined ? (
              <span className="text-[10px] text-muted">Not specified</span>
            ) : (
              <JsonValue value={question.criteria.true} />
            )}
          </div>
          <div
            role="group"
            aria-label={`${id} no criterion`}
            className="grid min-w-0 content-start gap-1.5 rounded-md border border-line p-2"
          >
            <h5 className="m-0 text-[11px] font-semibold">
              No <span className="font-mono font-normal">(false)</span>
            </h5>
            {question.criteria.false === undefined ? (
              <span className="text-[10px] text-muted">Not specified</span>
            ) : (
              <JsonValue value={question.criteria.false} />
            )}
          </div>
        </div>
      )}
    </QuestionFrame>
  );
}

function ScoreQuestion({
  id,
  question,
}: {
  id: string;
  question: Extract<ClassifierQuestion, { type: "score" }>;
}) {
  return (
    <QuestionFrame id={id} question={question}>
      <p className="m-0 text-[10px] text-muted">
        Ordered levels · the result is a probability-weighted position.
      </p>
      <ol
        start={0}
        aria-label={`${id} ordered levels`}
        className="m-0 grid min-w-0 list-none gap-2 p-0"
      >
        {question.criteria.map((criterion, level) => (
          <li
            key={level}
            className="grid min-w-0 grid-cols-[1.5rem_minmax(0,1fr)] items-start gap-2"
          >
            <span
              aria-hidden="true"
              className="flex size-6 items-center justify-center rounded-full bg-classifier-light font-mono text-[11px] text-classifier"
            >
              {level}
            </span>
            <div className="grid min-w-0 gap-1.5 border-l border-classifier/20 pl-2">
              <h5 className="m-0 text-[10px] font-medium text-muted">Level {level}</h5>
              <JsonValue value={criterion} />
            </div>
          </li>
        ))}
      </ol>
    </QuestionFrame>
  );
}

export function ClassifierQuestions({ declaration }: { declaration: ClassifierDeclaration }) {
  return (
    <div className="classifier-questions grid min-w-0 gap-3">
      <dl className="m-0 grid min-w-0 grid-cols-[repeat(auto-fit,minmax(min(100%,8rem),1fr))] gap-2">
        <DetailValue label="Requested model">{declaration.runtime.model}</DetailValue>
        <DetailValue label="Timeout">{declaration.runtime.timeout} seconds</DetailValue>
      </dl>
      {Object.entries(declaration.questions).map(([id, question]) => {
        switch (question.type) {
          case "choice":
            return <ChoiceQuestion key={id} id={id} question={question} />;
          case "noul":
            return <NoulQuestion key={id} id={id} question={question} />;
          case "score":
            return <ScoreQuestion key={id} id={id} question={question} />;
        }
      })}
    </div>
  );
}

function Probability({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <meter
        min={0}
        max={1}
        value={value}
        aria-label={label}
        className="h-2 min-w-16 max-w-full flex-1 accent-classifier"
      />
      <span className="min-w-0 font-mono text-[10px] [overflow-wrap:anywhere]">{value}</span>
    </div>
  );
}

function ChoiceAnswer({
  id,
  answer,
}: {
  id: string;
  answer: Extract<ClassifierAnswer, { type: "choice" }>;
}) {
  return (
    <>
      <dl className="m-0 grid min-w-0 grid-cols-[minmax(0,1fr)_auto] gap-3">
        <DetailValue label="Selected option">
          <span className="font-mono font-semibold text-classifier">{answer.choice}</span>
        </DetailValue>
        <DetailValue label="Confidence">
          <span className="font-mono">{answer.confidence}</span>
        </DetailValue>
      </dl>
      <div
        className="grid min-w-0 gap-2"
        role="group"
        aria-label={`${id} probability distribution`}
      >
        <h5 className="m-0 text-[10px] font-medium text-muted">
          Option probabilities · 0 to 1
        </h5>
        <dl className="m-0 grid min-w-0 gap-2">
          {Object.entries(answer.probabilities).map(([option, probability]) => (
            <div
              key={option}
              className={`grid min-w-0 gap-1.5 rounded-md border p-2 ${option === answer.choice ? "border-classifier/30 bg-classifier-light" : "border-line"}`}
            >
              <dt className="flex min-w-0 items-start gap-1 text-[11px] [overflow-wrap:anywhere]">
                {option === answer.choice && (
                  <Check aria-hidden="true" className="size-3 shrink-0 text-classifier" />
                )}
                <span className="min-w-0">{option}</span>
              </dt>
              <dd className="m-0 min-w-0">
                <Probability label={`${id}: ${option} probability`} value={probability} />
              </dd>
            </div>
          ))}
        </dl>
      </div>
    </>
  );
}

function NoulAnswer({
  id,
  answer,
}: {
  id: string;
  answer: Extract<ClassifierAnswer, { type: "noul" }>;
}) {
  return (
    <div className="grid min-w-0 gap-2 rounded-md bg-classifier-light p-3">
      <dl className="m-0 min-w-0">
        <DetailValue label="Probability of yes">
          <span className="font-mono text-lg text-classifier">{answer.noul}</span>
        </DetailValue>
      </dl>
      <meter
        min={0}
        max={1}
        value={answer.noul}
        aria-label={`${id}: probability of yes`}
        className="h-2 w-full accent-classifier"
      />
      <div className="flex justify-between gap-2 text-[10px] text-muted">
        <span>No · 0</span>
        <span>Yes · 1</span>
      </div>
    </div>
  );
}

function ScoreAnswer({
  id,
  answer,
}: {
  id: string;
  answer: Extract<ClassifierAnswer, { type: "score" }>;
}) {
  const maximum = Object.keys(answer.legend).length - 1;
  return (
    <>
      <div className="grid min-w-0 gap-2 rounded-md bg-classifier-light p-3">
        <dl className="m-0 grid min-w-0 grid-cols-[minmax(0,1fr)_auto] gap-3">
          <DetailValue label="Score">
            <span className="font-mono text-lg text-classifier">{answer.score}</span>
          </DetailValue>
          <DetailValue label="Confidence">
            <span className="font-mono">{answer.confidence}</span>
          </DetailValue>
        </dl>
        <meter
          min={0}
          max={maximum}
          value={answer.score}
          aria-label={`${id}: weighted score`}
          className="h-2 w-full accent-classifier"
        />
        <div className="flex justify-between gap-2 text-[10px] text-muted">
          <span>Level 0</span>
          <span>Level {maximum}</span>
        </div>
        <p className="m-0 text-[10px] text-muted">
          Probability-weighted position, not a selected level.
        </p>
      </div>
      <div
        className="grid min-w-0 gap-2"
        role="group"
        aria-label={`${id} probability distribution`}
      >
        <h5 className="m-0 text-[10px] font-medium text-muted">Level probabilities · 0 to 1</h5>
        <ol start={0} className="m-0 grid min-w-0 list-none gap-2 p-0">
          {Object.entries(answer.probabilities).map(([level, probability]) => (
            <li
              key={level}
              className="grid min-w-0 gap-1.5 border-l-2 border-classifier/25 pl-2"
            >
              <h6 className="m-0 text-[10px] font-medium text-classifier">Level {level}</h6>
              <JsonValue value={answer.legend[level]} />
              <Probability label={`${id}: ${level} probability`} value={probability} />
            </li>
          ))}
        </ol>
      </div>
    </>
  );
}

function AnswerDetails({ id, answer }: { id: string; answer: ClassifierAnswer }) {
  return (
    <section
      aria-label={`Answer ${id}`}
      className="grid min-w-0 gap-3 rounded-lg border border-line p-3"
    >
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <h4 className="m-0 min-w-0 text-xs font-semibold [overflow-wrap:anywhere]">{id}</h4>
        <QuestionKind kind={answer.type} />
      </div>
      {answer.type === "choice" ? (
        <ChoiceAnswer id={id} answer={answer} />
      ) : answer.type === "noul" ? (
        <NoulAnswer id={id} answer={answer} />
      ) : (
        <ScoreAnswer id={id} answer={answer} />
      )}
    </section>
  );
}

export function ClassifierInvocationDetails({
  invocation,
  statusOverride,
}: {
  invocation: ClassifierInvocation;
  statusOverride?: "interrupted" | "unknown";
}) {
  const status = statusOverride ?? invocation.status;
  return (
    <section
      className="classifier-invocation grid min-w-0 gap-4"
      aria-label={`Classifier invocation ${invocation.invocation_id}`}
    >
      <dl className="m-0 grid min-w-0 grid-cols-[repeat(auto-fit,minmax(min(100%,8rem),1fr))] gap-3">
        <DetailValue label="Outcome">
          <span
            className={`capitalize ${status === "failed" ? "text-danger" : status === "success" ? "text-mint" : "text-muted"}`}
          >
            {status}
          </span>
        </DetailValue>
        <DetailValue label="Requested model">
          {invocation.declaration.runtime.model}
        </DetailValue>
        {invocation.result !== null && (
          <>
            <DetailValue label="Model">{invocation.result.model}</DetailValue>
            <DetailValue label="Input tokens">
              {invocation.result.usage.input_tokens}
            </DetailValue>
            <DetailValue label="Output tokens">
              {invocation.result.usage.output_tokens}
            </DetailValue>
          </>
        )}
      </dl>
      <section aria-label="Input state" className="grid min-w-0 gap-2">
        <h4 className="m-0 text-[11px] font-semibold">Input state</h4>
        {invocation.input === null ? (
          <p className="m-0 text-[11px] text-muted">
            Not captured: the state failed validation before the request.
          </p>
        ) : (
          <JsonValue value={invocation.input} />
        )}
      </section>
      <section aria-label="Result" className="grid min-w-0 gap-2 border-t border-line pt-3">
        <h4 className="m-0 text-[11px] font-semibold">Result</h4>
        {invocation.error !== null && (
          <div
            role="alert"
            className="min-w-0 rounded-md border border-danger/25 bg-canvas p-2 text-[11px] text-danger"
          >
            <p className="m-0 whitespace-pre-wrap [overflow-wrap:anywhere]">
              {invocation.error}
            </p>
          </div>
        )}
        {invocation.result === null ? (
          <p className="m-0 text-[11px] text-muted" role="status">
            {status === "running"
              ? "Classification is running; answers are not available yet."
              : statusOverride
                ? "No terminal result was retained for this call."
                : "No classification answers were returned."}
          </p>
        ) : (
          <div className="grid min-w-0 gap-3" role="group" aria-label="Classification answers">
            {Object.entries(invocation.result.answers).map(([id, answer]) => (
              <AnswerDetails key={id} id={id} answer={answer} />
            ))}
          </div>
        )}
      </section>
      <details className="min-w-0 border-t border-line pt-2 text-[10px] text-muted">
        <summary className="cursor-pointer rounded-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-classifier">
          Call metadata
        </summary>
        <dl className="m-0 mt-2 grid min-w-0 gap-2">
          <DetailValue label="Invocation ID">{invocation.invocation_id}</DetailValue>
          <DetailValue label="Call">{invocation.invocation_index + 1}</DetailValue>
        </dl>
      </details>
    </section>
  );
}
