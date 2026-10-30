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
}

/**
 * One input draft for a workflow, shared by the run controls' JSON editor and the
 * graph's input popup. It starts from the input the schedule already uses or the
 * declared defaults, and follows those as they change elsewhere until it is edited.
 */
export function useInputDraft(workflow: FlowInfoMsg | undefined): InputDraft {
  const seed = inputDraftSeed(workflow);
  const [text, setText] = useState(seed);
  const workflowId = workflow?.workflowId;
  const draftWorkflow = useRef(workflowId);
  const seededDraft = useRef(seed);
  useLayoutEffect(() => {
    const previousSeed = seededDraft.current;
    seededDraft.current = seed;
    if (draftWorkflow.current !== workflowId) {
      draftWorkflow.current = workflowId;
      setText(seed);
      return;
    }
    if (seed !== previousSeed && text === previousSeed) setText(seed);
  }, [seed, text, workflowId]);
  // Stable between edits, so the graph does not rebuild its nodes on every render.
  return useMemo(() => ({ text, seed, setText }), [text, seed]);
}
