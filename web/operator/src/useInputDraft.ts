import { useLayoutEffect, useMemo, useRef, useState } from "react";

import type { FlowInfoMsg } from "./model";
import { workflowInputDraft } from "./workflowInput";

/** The JSON text a workflow's input starts from: its schedule's input or its defaults. */
export function inputDraftSeed(workflow: FlowInfoMsg | undefined): string {
  return JSON.stringify((workflow && workflowInputDraft(workflow)) ?? {}, null, 2);
}

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
}

/**
 * One input draft for a workflow, shared by the run controls and the input panel. It starts from the input the schedule already uses or the
 * declared defaults, and follows those as they change elsewhere until it is edited.
 */
export function useInputDraft(workflow: FlowInfoMsg | undefined): InputDraft {
  const seed = inputDraftSeed(workflow);
  const [text, setText] = useState(seed);
  const [pending, setPending] = useState<Record<string, string>>({});
  const workflowId = workflow?.workflowId;
  const draftWorkflow = useRef(workflowId);
  const seededDraft = useRef(seed);
  useLayoutEffect(() => {
    const previousSeed = seededDraft.current;
    seededDraft.current = seed;
    // A replaced draft drops text typed against the old one.
    if (draftWorkflow.current !== workflowId) {
      draftWorkflow.current = workflowId;
      setText(seed);
      setPending({});
      return;
    }
    if (seed !== previousSeed && text === previousSeed) {
      setText(seed);
      setPending({});
    }
  }, [seed, text, workflowId]);
  // Stable between edits, so the graph does not rebuild its nodes on every render.
  return useMemo(() => ({ text, seed, setText, pending, setPending }), [text, seed, pending]);
}
