import { useEffect, useId, useRef, useState } from "react";
import { ChevronRight, X } from "lucide-react";

import type { AgentDeclaration, AgentFieldSchemas, ToolMetadata } from "./GraphCanvas";
import { Markdown } from "./Markdown";
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

export function InspectorResources({ declaration }: { declaration: AgentDeclaration }) {
  const [selectedSkillName, setSelectedSkillName] = useState<string>();
  const selectedSkill = declaration.skills.find((skill) => skill.name === selectedSkillName);
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
      {declaration.skills.length > 0 && (
        <section aria-label="Skills">
          <h3 className="inspector-section-title">Skills</h3>
          {declaration.skills.map((skill) => (
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
          {selectedSkill && (
            <div className="grid min-w-0 gap-6 p-6 pr-16">
              <Markdown full>{selectedSkill.instructions}</Markdown>
              {(["packages", "modules"] as const).map((kind) =>
                selectedSkill[kind].length > 0 ? (
                  <section key={kind} aria-label={kind === "packages" ? "Packages" : "Modules"}>
                    <h3 className="inspector-section-title">
                      {kind === "packages" ? "Packages" : "Modules"}
                    </h3>
                    <ul className="m-0 grid list-none gap-2 p-0">
                      {selectedSkill[kind].map((name, index) => (
                        <li key={`${index}:${name}`} className="min-w-0">
                          <code className="text-xs text-secondary [overflow-wrap:anywhere]">
                            {name}
                          </code>
                        </li>
                      ))}
                    </ul>
                  </section>
                ) : null,
              )}
            </div>
          )}
        </div>
      </dialog>
    </>
  );
}
