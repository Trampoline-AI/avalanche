import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ArrowLeft, Eye, X } from "lucide-react";

import { parseAgentDeclaration, type SkillMetadata } from "./GraphCanvas";
import type { FlowInfoMsg } from "./model";
import { SkillContent } from "./SkillContent";

interface SkillUsage {
  workflowId: string;
  workflowName: string;
  nodeId: string;
  nodeName: string;
}

interface CatalogSkill {
  key: string;
  skill: SkillMetadata;
  usages: SkillUsage[];
}

function collectSkills(workflows: FlowInfoMsg[]): CatalogSkill[] {
  const entries = new Map<string, CatalogSkill>();
  for (const workflow of workflows) {
    for (const [nodeId, raw] of Object.entries(workflow.agentMetadataJson)) {
      for (const skill of parseAgentDeclaration(raw)?.skills ?? []) {
        const key = JSON.stringify([
          skill.name,
          skill.instructions,
          [...skill.packages].sort(),
          [...skill.modules].sort(),
        ]);
        let entry = entries.get(key);
        if (!entry) {
          entry = { key, skill, usages: [] };
          entries.set(key, entry);
        }
        if (
          !entry.usages.some(
            (usage) => usage.workflowId === workflow.workflowId && usage.nodeId === nodeId,
          )
        ) {
          entry.usages.push({
            workflowId: workflow.workflowId,
            workflowName: workflow.displayName,
            nodeId,
            nodeName: workflow.displayNames[nodeId] ?? nodeId,
          });
        }
      }
    }
  }
  return [...entries.values()].sort((a, b) => a.skill.name.localeCompare(b.skill.name));
}

export function SkillCatalog({
  workflows,
  onOpenNode,
}: {
  workflows: FlowInfoMsg[];
  onOpenNode: (workflowId: string, nodeId: string) => void;
}) {
  const entries = useMemo(() => collectSkills(workflows), [workflows]);
  const [selectedKey, setSelectedKey] = useState<string>();
  const selected = entries.find((entry) => entry.key === selectedKey);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = dialogRef.current;
    return () => {
      if (dialog?.open) dialog.close();
    };
  }, []);
  useEffect(() => {
    if (selected) backRef.current?.focus();
    else if (dialogRef.current?.open) listRef.current?.focus();
  }, [selected]);

  const nodeLink = (usage: SkillUsage) => (
    <button
      type="button"
      title={usage.workflowName}
      className="cursor-pointer border-0 bg-transparent p-0 text-left text-xs text-muted hover:text-acid hover:underline focus-visible:outline-2 focus-visible:outline-acid"
      onClick={() => {
        dialogRef.current?.close();
        onOpenNode(usage.workflowId, usage.nodeId);
      }}
    >
      {usage.nodeName}
    </button>
  );

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="dialog"
        className="cursor-pointer border-0 bg-transparent p-0 font-mono text-[11px] whitespace-nowrap text-secondary hover:text-acid focus-visible:outline-2 focus-visible:outline-acid"
        onClick={() => dialogRef.current?.showModal()}
      >
        Skills: {entries.length}
      </button>
      <dialog
        ref={dialogRef}
        aria-labelledby={titleId}
        className="skill-markdown-dialog m-auto max-h-[85dvh] w-[min(52rem,calc(100vw-2rem))] overflow-hidden rounded-xl border border-line bg-panel p-0 text-ink shadow-xl backdrop:bg-black/40"
        onClose={() => {
          setSelectedKey(undefined);
          triggerRef.current?.focus();
        }}
      >
        <div className="flex items-center gap-3 border-b border-line px-6 py-4">
          {selected && (
            <button
              ref={backRef}
              type="button"
              aria-label="Back to skills"
              className="cursor-pointer border-0 bg-transparent p-1 text-secondary hover:text-acid focus-visible:outline-2 focus-visible:outline-acid"
              onClick={() => {
                setSelectedKey(undefined);
              }}
            >
              <ArrowLeft className="size-4" aria-hidden="true" />
            </button>
          )}
          <h2
            id={titleId}
            className="m-0 min-w-0 flex-1 text-base font-semibold [overflow-wrap:anywhere]"
          >
            {selected ? selected.skill.name : "Workflow skills"}
          </h2>
          <button
            type="button"
            aria-label="Close skills"
            className="icon-button grid size-[30px] cursor-pointer place-items-center rounded-[7px] border border-line bg-panel p-0 text-secondary focus-visible:outline-2 focus-visible:outline-acid"
            onClick={() => dialogRef.current?.close()}
          >
            <X className="size-4" aria-hidden="true" />
          </button>
        </div>
        <div className="max-h-[calc(85dvh-5rem)] overflow-y-auto overscroll-contain">
          <div ref={listRef} tabIndex={-1} hidden={selected !== undefined} className="p-6">
            <p className="mb-4 text-xs text-muted">
              Skills used across workflows in this workspace.
            </p>
            {entries.length === 0 && (
              <p className="text-sm text-secondary">
                No skills declared in the loaded workflows.
              </p>
            )}
            <ul className="m-0 grid list-none gap-2 p-0">
              {entries.map((entry) => {
                const variants = entries.filter((item) => item.skill.name === entry.skill.name);
                return (
                  <li key={entry.key}>
                    <div className="flex w-full items-center gap-3 rounded-lg border border-line p-3 text-left">
                      <div className="min-w-0 flex-1">
                        <strong className="block text-sm [overflow-wrap:anywhere]">
                          {entry.skill.name}
                          {variants.length > 1
                            ? ` · variant ${variants.indexOf(entry) + 1}`
                            : ""}
                        </strong>
                        <ul className="mt-1 flex list-none flex-wrap gap-x-3 gap-y-1 p-0">
                          {entry.usages.map((usage) => (
                            <li key={`${usage.workflowId}\0${usage.nodeId}`}>
                              {nodeLink(usage)}
                            </li>
                          ))}
                        </ul>
                      </div>
                      <button
                        type="button"
                        aria-label={`View ${entry.skill.name}${variants.length > 1 ? ` variant ${variants.indexOf(entry) + 1}` : ""}`}
                        className="grid size-8 shrink-0 cursor-pointer place-items-center rounded-md border-0 bg-transparent text-muted hover:bg-canvas hover:text-acid focus-visible:outline-2 focus-visible:outline-acid"
                        onClick={() => setSelectedKey(entry.key)}
                      >
                        <Eye aria-hidden="true" className="size-4" />
                      </button>
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
          {selected && (
            <>
              <section aria-label="Used by" className="border-b border-line px-6 py-4">
                <h3 className="inspector-section-title">Used by</h3>
                <ul className="m-0 grid list-none gap-1 p-0 text-xs text-secondary">
                  {selected.usages.map((usage) => (
                    <li key={`${usage.workflowId}\0${usage.nodeId}`}>{nodeLink(usage)}</li>
                  ))}
                </ul>
              </section>
              <SkillContent skill={selected.skill} />
            </>
          )}
        </div>
      </dialog>
    </>
  );
}
