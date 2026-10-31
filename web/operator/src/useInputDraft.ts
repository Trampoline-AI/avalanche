import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { OperatorApi } from "./api";
import { isUnknownRecord } from "./guards";
import type { FlowInfoMsg, InputFileAttachment, StagedInputFile } from "./model";
import { workflowInputDraft, type FileFieldKind } from "./workflowInput";

/** The JSON text a workflow's input starts from: its schedule's input or its defaults. */
export function inputDraftSeed(workflow: FlowInfoMsg | undefined): string {
  return JSON.stringify((workflow && workflowInputDraft(workflow)) ?? {}, null, 2);
}

/** Files picked for one field: still uploading, staged, or refused by the host. */
export type FieldFiles =
  | { status: "staging"; kind: FileFieldKind; names: string[] }
  | { status: "staged"; kind: FileFieldKind; files: StagedInputFile[] }
  | { status: "failed"; kind: FileFieldKind; names: string[]; error: string };

export interface InputDraft {
  /** JSON text of the input Run and the schedule start with. */
  text: string;
  /** The text the draft was last seeded with; equal to `text` until someone edits it. */
  seed: string;
  setText: (text: string) => void;
  /**
   * Text typed into a field that does not parse yet, by field name. The draft keeps
   * the field's last valid value, so Run and the schedule refuse to start while any
   * text is pending.
   */
  pending: Readonly<Record<string, string>>;
  setPending: (update: (current: Record<string, string>) => Record<string, string>) => void;
  /** Files picked for file fields, by field name. They replace the field's JSON value. */
  files: Readonly<Record<string, FieldFiles>>;
  /**
   * Stage the picked files for a field right away, replacing earlier picks.
   * Undefined when the host cannot stage files, so file fields are not editable.
   */
  pickFiles?: (field: string, kind: FileFieldKind, picked: File[]) => void;
  /** Forget a field's picked files and its value, so it takes its default. */
  clearFiles: (field: string) => void;
}

/**
 * One input draft for a workflow, shared by the run controls and the input panel. It starts from the input the schedule already uses or the
 * declared defaults, and follows those as they change elsewhere until it is edited.
 */
export function useInputDraft(
  workflow: FlowInfoMsg | undefined,
  api?: Pick<OperatorApi, "stageInputFile">,
): InputDraft {
  const seed = inputDraftSeed(workflow);
  const [text, setText] = useState(seed);
  const [pending, setPending] = useState<Record<string, string>>({});
  const [files, setFiles] = useState<Record<string, FieldFiles>>({});
  const workflowId = workflow?.workflowId;
  const draftWorkflow = useRef(workflowId);
  const seededDraft = useRef(seed);
  // The latest pick per field; an older upload that finishes later is dropped.
  const picks = useRef<Record<string, number>>({});
  const nextPick = useRef(0);
  useLayoutEffect(() => {
    const previousSeed = seededDraft.current;
    seededDraft.current = seed;
    // A replaced draft drops text typed and files picked against the old one.
    if (draftWorkflow.current !== workflowId) {
      draftWorkflow.current = workflowId;
      picks.current = {};
      setText(seed);
      setPending({});
      setFiles({});
      return;
    }
    if (seed !== previousSeed && text === previousSeed && Object.keys(files).length === 0) {
      setText(seed);
      setPending({});
    }
  }, [seed, text, files, workflowId]);

  const pickFiles = useMemo(() => {
    if (!api?.stageInputFile || workflowId === undefined) return undefined;
    const stageInputFile = api.stageInputFile.bind(api);
    return (field: string, kind: FileFieldKind, picked: File[]) => {
      const pick = ++nextPick.current;
      picks.current[field] = pick;
      const names = picked.map((file) => file.name);
      const settle = (next: FieldFiles) => {
        if (picks.current[field] !== pick) return;
        setFiles((current) => ({ ...current, [field]: next }));
      };
      settle({ status: "staging", kind, names });
      Promise.all(
        picked.map((file) => stageInputFile(file, { workflow: workflowId, field })),
      ).then(
        (staged) => settle({ status: "staged", kind, files: staged }),
        (reason: unknown) =>
          settle({
            status: "failed",
            kind,
            names,
            error: reason instanceof Error ? reason.message : "The file could not be uploaded",
          }),
      );
    };
  }, [api, workflowId]);

  const clearFiles = useCallback((field: string) => {
    delete picks.current[field];
    setFiles(({ [field]: _, ...rest }) => rest);
    setText((current) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(current);
      } catch {
        // The panel shows the draft is not JSON; leave the text for the person to fix.
        return current;
      }
      if (!isUnknownRecord(parsed)) return current;
      const rest = { ...parsed };
      delete rest[field];
      return JSON.stringify(rest, null, 2);
    });
  }, []);

  // Stable between edits, so the graph does not rebuild its nodes on every render.
  return useMemo(
    () => ({ text, seed, setText, pending, setPending, files, pickFiles, clearFiles }),
    [text, seed, pending, files, pickFiles, clearFiles],
  );
}

/**
 * The input and file attachments a run or schedule starts with. A staged list
 * field becomes an empty list the operator appends its files to; a single file
 * field is left out of the JSON, since its attachment sets it.
 */
export function applyInputFiles(
  input: Record<string, unknown>,
  files: Readonly<Record<string, FieldFiles>>,
): { input: Record<string, unknown>; inputFiles: InputFileAttachment[] } {
  const next = { ...input };
  const inputFiles: InputFileAttachment[] = [];
  for (const [fieldName, entry] of Object.entries(files)) {
    if (entry.status !== "staged") {
      throw new Error(
        entry.status === "staging"
          ? `Wait for ${fieldName} to finish uploading`
          : `Pick the files for ${fieldName} again: ${entry.error}`,
      );
    }
    if (entry.kind === "list") next[fieldName] = [];
    else delete next[fieldName];
    inputFiles.push(...entry.files.map((file) => ({ ...file, fieldName })));
  }
  return { input: next, inputFiles };
}
