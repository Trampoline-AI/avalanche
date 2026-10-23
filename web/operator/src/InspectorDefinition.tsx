import { useEffect, useId, useRef, useState } from "react";
import { ChevronRight, X } from "lucide-react";

import type {
  AgentDeclaration,
  AgentFieldSchemas,
  SkillMetadata,
  ToolMetadata,
} from "./GraphCanvas";
import { SkillContent } from "./SkillContent";
import { PythonSource } from "./PythonSource";
import { ValueView } from "./ValueView";

export function InspectorFields({
  fields,
  values,
  unavailableMessage = "Schema unavailable.",
}: {
  fields: AgentFieldSchemas["inputs"] | undefined;
  values?: Record<string, unknown>;
  unavailableMessage?: string;
}) {
  const declaredNames = new Set(fields?.map((field) => field.name) ?? []);
  const retainedOnlyValues = Object.entries(values ?? {}).filter(
    ([name]) => !declaredNames.has(name),
  );
  if (fields === undefined && retainedOnlyValues.length === 0)
    return <p className="text-[11px] text-muted">{unavailableMessage}</p>;
  if (fields?.length === 0 && retainedOnlyValues.length === 0)
    return <p className="text-[11px] text-muted">No declared fields.</p>;
  return (
    <div className="declared-fields grid min-w-0 gap-3">
      {(fields ?? []).map((field) => (
        <div
          className="field-detail min-w-0 [&_strong]:block [&_strong]:text-[10px] [&_code]:mt-0.5 [&_code]:block [&_code]:text-[9px] [&_code]:text-muted [&_code]:[overflow-wrap:anywhere] [&_p]:mt-1 [&_p]:mb-0 [&_p]:text-[10px] [&_p]:text-muted [&_p]:[overflow-wrap:anywhere]"
          key={field.name}
        >
          <strong>{field.name}</strong>
          {field.type && <code>{field.type}</code>}
          {field.description && <p>{field.description}</p>}
          {values && Object.hasOwn(values, field.name) && (
            <div
              className="mt-2 min-w-0 rounded-md bg-canvas p-2"
              aria-label={`${field.name} value`}
            >
              <ValueView value={values[field.name]} />
            </div>
          )}
        </div>
      ))}
      {retainedOnlyValues.map(([name, value]) => (
        <div
          className="field-detail min-w-0 [&_strong]:block [&_strong]:text-[10px] [&_p]:mt-1 [&_p]:mb-0 [&_p]:text-[10px] [&_p]:text-muted"
          key={`retained-${name}`}
        >
          <strong>{name}</strong>
          {fields === undefined ? (
            <p>{unavailableMessage}</p>
          ) : (
            <p>Retained value is not present in the historical schema.</p>
          )}
          <div className="mt-2 min-w-0 rounded-md bg-canvas p-2" aria-label={`${name} value`}>
            <ValueView value={value} />
          </div>
        </div>
      ))}
    </div>
  );
}

function ToolDetails({ tool }: { tool: ToolMetadata }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="min-w-0 py-2 text-xs text-secondary first-of-type:pt-0"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer font-medium focus-visible:outline-2 focus-visible:outline-acid">
        {tool.name}
      </summary>
      {open &&
        (tool.sourceCode ? (
          <div
            className="mt-2 grid max-h-[min(14rem,45vh)] min-h-0 min-w-0 grid-rows-[minmax(0,1fr)] overflow-hidden rounded-md border border-line bg-canvas"
            role="region"
            aria-label={`${tool.name} source`}
          >
            <PythonSource source={tool.sourceCode} />
          </div>
        ) : (
          <p className="mt-2 text-[11px] text-muted">
            Source code is unavailable for this tool.
          </p>
        ))}
    </details>
  );
}

function ModelName({ label, model }: { label: string; model?: string }) {
  const [revealed, setRevealed] = useState(false);
  const tooltipId = useId();
  const separator = model?.indexOf("/") ?? -1;
  const prefix = model?.slice(0, separator + 1) ?? "";
  const name = model?.slice(separator + 1) || "—";

  return (
    <span
      className="relative min-w-0 flex-1"
      tabIndex={model ? 0 : undefined}
      aria-describedby={revealed && model ? tooltipId : undefined}
      onMouseEnter={() => setRevealed(true)}
      onMouseLeave={() => setRevealed(false)}
      onFocus={() => setRevealed(true)}
      onBlur={() => setRevealed(false)}
      onKeyDown={(event) => {
        if (event.key === "Escape") setRevealed(false);
      }}
    >
      <span aria-hidden="true">&nbsp;</span>
      <span
        id={tooltipId}
        role={revealed && model ? "tooltip" : undefined}
        className={`absolute top-0 left-0 bg-panel ${revealed ? "z-50 pr-2" : "max-w-full overflow-hidden text-ellipsis"}`}
      >
        <span className="text-muted">{label}:</span>{" "}
        <span
          aria-hidden={!revealed}
          className="inline-block overflow-hidden align-bottom transition-[width,opacity] duration-150 ease-out motion-reduce:transition-none"
          style={{ width: revealed ? `${prefix.length}ch` : "0ch", opacity: revealed ? 1 : 0 }}
        >
          {prefix}
        </span>
        <span>{name}</span>
      </span>
    </span>
  );
}

export function InspectorAgentSummary({
  mainModel,
  subModel,
  skills,
}: {
  mainModel?: string;
  subModel?: string;
  skills?: SkillMetadata[];
}) {
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  const [selectedSkillName, setSelectedSkillName] = useState<string>();
  const selectedSkill = skills?.find((skill) => skill.name === selectedSkillName);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (selectedSkill && !dialog.open) dialog.showModal();
    else if (!selectedSkill && dialog.open) dialog.close();
  }, [selectedSkill]);

  useEffect(() => {
    const dialog = dialogRef.current;
    return () => {
      if (dialog?.open) dialog.close();
    };
  }, []);

  return (
    <>
      <div
        className="relative mt-2 flex min-w-0 items-center gap-3 whitespace-nowrap font-mono text-[10px] text-secondary"
        aria-label="Agent configuration"
      >
        <ModelName label="main" model={mainModel} />
        <ModelName label="sub" model={subModel} />
        {skills ? (
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={listId}
            onClick={() => setExpanded(!expanded)}
            className="shrink-0 cursor-pointer border-0 bg-transparent p-0 font-[inherit] text-acid hover:underline focus-visible:outline-2 focus-visible:outline-acid"
          >
            skills: {skills.length}
          </button>
        ) : (
          <span className="shrink-0" title="Skill metadata was not retained for this run.">
            skills: —
          </span>
        )}
      </div>
      {expanded && skills && (
        <section id={listId} aria-label="Skills" className="mt-3 max-h-48 overflow-y-auto">
          {skills.length === 0 && <p className="text-xs text-muted">No skills configured.</p>}
          {skills.map((skill) => (
            <button
              key={skill.name}
              type="button"
              aria-haspopup="dialog"
              onClick={(event) => {
                triggerRef.current = event.currentTarget;
                setSelectedSkillName(skill.name);
              }}
              className="flex w-full cursor-pointer items-center gap-1.5 border-0 bg-transparent py-2 text-left text-xs font-medium text-secondary first-of-type:pt-0 hover:text-acid focus-visible:outline-2 focus-visible:outline-acid"
            >
              <span className="min-w-0 [overflow-wrap:anywhere]">{skill.name}</span>
              <ChevronRight aria-hidden="true" className="size-3 shrink-0" />
            </button>
          ))}
        </section>
      )}
      <dialog
        ref={dialogRef}
        aria-labelledby={titleId}
        className="skill-markdown-dialog m-auto max-h-[85dvh] w-[min(52rem,calc(100vw-2rem))] overflow-hidden rounded-xl border border-line bg-panel p-0 text-ink shadow-xl backdrop:bg-black/40"
        onClose={() => {
          setSelectedSkillName(undefined);
          triggerRef.current?.focus();
        }}
      >
        <h2 id={titleId} className="sr-only">
          {selectedSkill?.name ?? "Skill"}
        </h2>
        <button
          type="button"
          aria-label="Close skill"
          onClick={() => dialogRef.current?.close()}
          className="icon-button absolute top-3 right-6 grid size-[30px] cursor-pointer place-items-center rounded-[7px] border border-line bg-panel p-0 text-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid"
        >
          <X aria-hidden="true" className="size-4" strokeWidth={1.8} />
        </button>
        <div className="skill-markdown-scroll max-h-[calc(85dvh-2px)] overflow-auto overscroll-contain">
          {selectedSkill && <SkillContent skill={selectedSkill} />}
        </div>
      </dialog>
    </>
  );
}

export function InspectorResources({ declaration }: { declaration: AgentDeclaration }) {
  return (
    <>
      {declaration.tools.length > 0 && (
        <section aria-label="Tools">
          <h3 className="inspector-section-title">Tools</h3>
          {declaration.tools.map((tool) => (
            <ToolDetails key={tool.name} tool={tool} />
          ))}
        </section>
      )}
      {declaration.runtime !== undefined && (
        <details className="min-w-0">
          <summary className="cursor-pointer text-xs font-medium text-secondary focus-visible:outline-2 focus-visible:outline-acid">
            Runtime
          </summary>
          <div className="pt-3">
            <ValueView value={declaration.runtime} />
          </div>
        </details>
      )}
    </>
  );
}
