import { json } from "@codemirror/lang-json";
import { foldGutter, foldKeymap } from "@codemirror/language";
import { EditorState } from "@codemirror/state";
import { EditorView, keymap, lineNumbers } from "@codemirror/view";
import { githubLightInit } from "@uiw/codemirror-theme-github";
import { useEffect, useId, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import type {
  ClassifierAnswer,
  ClassifierDeclaration,
  ClassifierEntry,
  ClassifierInvocation,
  ClassifierQuestion,
} from "./classifier";
import { ValueView } from "./ValueView";
import { DeclaredSchema, StepInterfacePanel } from "./StepInterface";

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 0 });

const inputColorTheme = githubLightInit({ settings: { background: "var(--color-panel)" } });
const inputLayout = EditorView.theme({
  "&": { fontSize: "11px" },
  ".cm-content": { fontFamily: "var(--font-mono)", lineHeight: "1.65", padding: "8px 0" },
  ".cm-scroller": { maxHeight: "320px", overflow: "auto" },
  ".cm-gutters": {
    backgroundColor: "var(--color-panel)",
    color: "var(--color-muted)",
    border: "0",
  },
  "&.cm-focused": { outline: "1px solid var(--color-acid)" },
});

function ClassifierInput({ value }: { value: Exclude<ClassifierEntry, null> }) {
  const parent = useRef<HTMLDivElement>(null);
  const source = JSON.stringify(value, null, 2);
  useEffect(() => {
    if (parent.current === null) return;
    const view = new EditorView({
      parent: parent.current,
      state: EditorState.create({
        doc: source,
        extensions: [
          json(),
          inputColorTheme,
          inputLayout,
          lineNumbers(),
          foldGutter(),
          keymap.of(foldKeymap),
          EditorView.lineWrapping,
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          EditorView.contentAttributes.of({
            role: "textbox",
            "aria-label": "Classifier input JSON",
            "aria-readonly": "true",
            "aria-multiline": "true",
            tabindex: "0",
          }),
        ],
      }),
    });
    return () => view.destroy();
  }, [source]);
  return <div ref={parent} className="min-w-0 overflow-hidden rounded-md border border-line" />;
}

function JsonValue({ value }: { value: ClassifierEntry }) {
  return (
    <div className="classifier-value min-w-0 text-[11px]">
      <ValueView value={value} jsonOnly />
    </div>
  );
}

function DefinitionValue({ value }: { value: ClassifierEntry }) {
  return typeof value === "string" ? (
    <p className="m-0 text-[11px] leading-relaxed whitespace-pre-wrap text-secondary [overflow-wrap:anywhere]">
      {value}
    </p>
  ) : (
    <JsonValue value={value} />
  );
}

function Probability({ value }: { value: number }) {
  return <span className="shrink-0 tabular-nums">{percent.format(value)}</span>;
}

function AnswerSummary({
  id,
  answer,
  expanded,
}: {
  id: string;
  answer: ClassifierAnswer;
  expanded: boolean;
}) {
  if (answer.type === "noul") {
    return (
      <div className="grid gap-1.5">
        <div className="text-xs tabular-nums">
          <span className="font-semibold text-classifier">{percent.format(answer.noul)}</span>{" "}
          <span className="text-muted">true</span>
        </div>
        <div
          role="meter"
          aria-label={`${id} probability of true`}
          aria-valuemin={0}
          aria-valuemax={1}
          aria-valuenow={answer.noul}
          aria-valuetext={`${percent.format(answer.noul)} true`}
          className="h-1 w-24 max-w-full overflow-hidden rounded-full bg-line"
        >
          <span
            className="block h-full rounded-full bg-classifier"
            style={{ width: `${answer.noul * 100}%` }}
          />
        </div>
      </div>
    );
  }
  return (
    <div className="grid min-w-0 gap-1.5">
      {answer.type === "choice" ? (
        <ul aria-label={`${id} probabilities`} className="m-0 grid list-none gap-1 p-0">
          {Object.entries(answer.probabilities)
            .sort(
              ([left, a], [right, b]) =>
                b - a || Number(right === answer.choice) - Number(left === answer.choice),
            )
            .slice(0, expanded ? undefined : 3)
            .map(([option, probability]) => (
              <li
                key={option}
                className={`flex min-w-0 items-baseline justify-between gap-2 text-[11px] ${option === answer.choice ? "font-semibold text-ink" : "text-secondary"}`}
              >
                <span className="min-w-0 [overflow-wrap:anywhere]">{option}</span>
                <Probability value={probability} />
              </li>
            ))}
        </ul>
      ) : (
        <div className="text-xs tabular-nums">
          <span className="font-semibold text-classifier">{answer.score}</span>{" "}
          <span className="text-muted">of {Object.keys(answer.legend).length - 1}</span>
        </div>
      )}
      <p className="m-0 text-[10px] text-muted">
        Confidence: {percent.format(answer.confidence)}
      </p>
    </div>
  );
}

function QuestionCriteria({
  id,
  question,
  answer,
}: {
  id: string;
  question: ClassifierQuestion;
  answer?: ClassifierAnswer;
}) {
  if (question.type === "noul") {
    return (
      <dl className="m-0 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-2">
        {(["true", "false"] as const).map((key) => {
          const value = question.criteria?.[key];
          return (
            <div
              key={key}
              className="contents"
              role="group"
              aria-label={`${id} ${key} criterion`}
            >
              <dt className="text-[11px] font-medium text-secondary">
                {key === "true" ? "True" : "False"}
              </dt>
              <dd className="m-0 min-w-0">
                {value !== undefined && <DefinitionValue value={value} />}
              </dd>
            </div>
          );
        })}
      </dl>
    );
  }
  const criteria =
    question.type === "choice"
      ? Object.entries(question.criteria)
      : question.criteria.map((criterion, level): [string, ClassifierEntry] => [
          String(level),
          criterion,
        ]);
  const entries = criteria.map(([option, criterion]) => (
    <li key={option} className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1">
      <h5 className="m-0 text-[11px] font-medium text-secondary [overflow-wrap:anywhere]">
        {option}
      </h5>
      {answer && answer.type !== "noul" && (
        <span className="text-[11px]">
          <Probability value={answer.probabilities[option]} />
        </span>
      )}
      {!answer && criterion !== null && (
        <div className="col-span-2 min-w-0">
          <DefinitionValue value={criterion} />
        </div>
      )}
    </li>
  ));
  return question.type === "choice" ? (
    <ul aria-label={`${id} options`} className="m-0 grid list-none gap-3 p-0">
      {entries}
    </ul>
  ) : (
    <ol start={0} aria-label={`${id} ordered levels`} className="m-0 grid list-none gap-3 p-0">
      {entries}
    </ol>
  );
}

function QuestionRow({
  id,
  question,
  answer,
}: {
  id: string;
  question: ClassifierQuestion;
  answer?: ClassifierAnswer;
}) {
  const [expanded, setExpanded] = useState(false);
  const detailsId = useId();
  const typeLabel =
    question.type === "choice" ? "Choice" : question.type === "noul" ? "Noul" : "Score";
  const count =
    question.type === "choice"
      ? Object.keys(question.criteria).length
      : question.type === "score"
        ? question.criteria.length
        : null;
  const expandable =
    answer?.type === "choice" ? count !== null && count > 3 : answer?.type !== "noul";
  return (
    <section
      aria-label={`${answer ? "Answer" : "Question"} ${id}`}
      className="classifier-question-row"
    >
      {expandable ? (
        <button
          type="button"
          aria-label={`${expanded ? "Collapse" : "Expand"} ${id} ${answer?.type === "choice" ? "options" : "criteria"}`}
          aria-expanded={expanded}
          aria-controls={detailsId}
          onClick={() => setExpanded(!expanded)}
          className="-m-1 mt-0 cursor-pointer self-start rounded border-0 bg-transparent p-1 text-muted hover:bg-canvas hover:text-classifier focus-visible:outline-2 focus-visible:outline-classifier"
        >
          {expanded ? (
            <ChevronDown className="size-3.5" />
          ) : (
            <ChevronRight className="size-3.5" />
          )}
        </button>
      ) : (
        <span aria-hidden="true" />
      )}
      <div
        className={`classifier-question-prompt min-w-0 ${answer ? "" : "classifier-question-prompt-only"}`}
      >
        <h4 className="m-0 text-[13px] font-semibold [overflow-wrap:anywhere]">{id}</h4>
        {!answer && question.instructions !== null && (
          <div className="mt-1">
            <DefinitionValue value={question.instructions} />
          </div>
        )}
      </div>
      {answer && (
        <div
          id={answer.type === "choice" ? detailsId : undefined}
          className="classifier-question-answer min-w-0"
        >
          <AnswerSummary id={id} answer={answer} expanded={expanded} />
        </div>
      )}
      <div className="classifier-question-kind flex flex-wrap items-center justify-end gap-x-2 gap-y-1 text-right text-[9px] text-muted">
        <span className="rounded border border-classifier/20 bg-classifier-light px-1.5 py-0.5 font-mono text-classifier">
          {typeLabel}
        </span>
        {count !== null && (
          <span className="whitespace-nowrap">
            {question.type === "choice"
              ? `${count} options`
              : `${count} levels · 0–${count - 1}`}
          </span>
        )}
      </div>
      {expanded && expandable && answer?.type !== "choice" && (
        <div
          id={detailsId}
          className="classifier-question-criteria min-w-0 border-l border-line py-1 pl-3"
        >
          <QuestionCriteria id={id} question={question} answer={answer} />
        </div>
      )}
    </section>
  );
}

export function ClassifierQuestions({
  declaration,
  context = "definition",
}: {
  declaration: ClassifierDeclaration;
  context?: "definition" | "invocation";
}) {
  if (declaration.questions === null) {
    return (
      <p className="m-0 text-[11px] text-muted">
        {context === "invocation"
          ? "Questions were not resolved for this invocation."
          : "Questions are supplied at runtime for each call."}
      </p>
    );
  }
  return (
    <div className="classifier-questions min-w-0">
      {Object.entries(declaration.questions).map(([id, question]) => (
        <QuestionRow key={id} id={id} question={question} />
      ))}
    </div>
  );
}

export function ClassifierDefinition({
  declaration,
  context = "definition",
}: {
  declaration: ClassifierDeclaration;
  context?: "definition" | "invocation";
}) {
  return (
    <div className="grid min-w-0 gap-5 py-3">
      <section aria-label="Classifier" className="min-w-0 border-l-2 border-classifier/40 pl-3">
        <section aria-label="Questions" className="min-w-0">
          <h3 className="inspector-section-title">
            {context === "definition" && declaration.questions !== null
              ? "Default questions"
              : "Questions"}
          </h3>
          {context === "definition" && declaration.questions !== null && (
            <p className="mt-0 mb-2 text-[11px] text-muted">
              Calls may replace these defaults with their own questions.
            </p>
          )}
          <ClassifierQuestions declaration={declaration} context={context} />
        </section>
        <div className="mt-3 border-t border-line pt-3">
          <section aria-label="Classifier input" className="min-w-0">
            <h4 className="mt-0 mb-2 font-mono text-[9px] tracking-[.08em] text-muted uppercase">
              Classifier input
            </h4>
            {declaration.input_schema === null ? (
              <p className="m-0 text-[11px] text-muted">
                No input model declared. Accepts JSON state.
              </p>
            ) : (
              <DeclaredSchema label="state" schema={declaration.input_schema} />
            )}
          </section>
        </div>
      </section>
      <StepInterfacePanel definition={declaration} />
    </div>
  );
}

export function ClassifierInvocationDetails({
  invocation,
  statusOverride,
}: {
  invocation: ClassifierInvocation;
  statusOverride?: "interrupted" | "unknown";
}) {
  const [definitionExpanded, setDefinitionExpanded] = useState(false);
  const definitionId = useId();
  const status = statusOverride ?? invocation.status;
  const result = invocation.result;
  const questions = invocation.declaration.questions;
  return (
    <section
      className="classifier-invocation grid min-w-0 gap-3"
      aria-label={`Classifier invocation ${invocation.invocation_id}`}
    >
      <section aria-label="Input state" className="min-w-0">
        <h4 className="m-0 mb-1.5 font-mono text-[9px] tracking-[.08em] text-muted uppercase">
          Input
        </h4>
        {invocation.input === null ? (
          <p className="m-0 text-[11px] text-muted">Input not captured.</p>
        ) : (
          <ClassifierInput value={invocation.input} />
        )}
      </section>
      <section aria-label="Result" className="min-w-0">
        {invocation.error !== null && (
          <p
            role="alert"
            className="m-0 mb-2 text-[11px] whitespace-pre-wrap text-danger [overflow-wrap:anywhere]"
          >
            {invocation.error}
          </p>
        )}
        {result === null || questions === null ? (
          invocation.error === null && (
            <p className="m-0 text-[11px] text-muted" role="status">
              {status === "running"
                ? "Running…"
                : statusOverride
                  ? "Result unavailable."
                  : status === "cancelled"
                    ? "Cancelled."
                    : "No output."}
            </p>
          )
        ) : (
          <div
            className="classifier-questions min-w-0"
            role="group"
            aria-label="Classification answers"
          >
            {Object.entries(questions).map(([id, question]) => (
              <QuestionRow key={id} id={id} question={question} answer={result.answers[id]} />
            ))}
          </div>
        )}
      </section>
      <section aria-label="Invocation definition" className="min-w-0 border-t border-line pt-2">
        <button
          type="button"
          aria-label={`${definitionExpanded ? "Collapse" : "Expand"} invocation definition`}
          aria-expanded={definitionExpanded}
          aria-controls={definitionId}
          onClick={() => setDefinitionExpanded(!definitionExpanded)}
          className="flex cursor-pointer items-center gap-1.5 rounded border-0 bg-transparent p-0 text-[11px] text-secondary hover:text-classifier focus-visible:outline-2 focus-visible:outline-classifier"
        >
          {definitionExpanded ? (
            <ChevronDown className="size-3.5" />
          ) : (
            <ChevronRight className="size-3.5" />
          )}
          Definition
        </button>
        {definitionExpanded && (
          <div id={definitionId}>
            <ClassifierDefinition declaration={invocation.declaration} context="invocation" />
          </div>
        )}
      </section>
    </section>
  );
}
