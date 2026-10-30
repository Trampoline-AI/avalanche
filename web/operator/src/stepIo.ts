import {
  StepValuePreviewKindV2,
  type StepIoV2,
  type StepValuePreviewV2,
} from "./generated/operator";
import { parseJsonValue, type JsonValue } from "./stepInterface";

/**
 * A bounded, display-only rendering of one value a standard step received or
 * returned. The operator replaces cut content with an explicit "…" marker.
 */
export type StepValuePreview =
  | { kind: "json"; typeName: string; value: JsonValue; truncated: boolean }
  | { kind: "repr"; typeName: string; text: string; truncated: boolean };

export interface StepParameterPreview {
  name: string;
  value: StepValuePreview;
}

/** The latest call's arguments, and its return value once the call has returned. */
export interface StepIo {
  inputs: StepParameterPreview[];
  output?: StepValuePreview;
}

function mapStepValuePreview(preview: StepValuePreviewV2, path: string): StepValuePreview {
  const parsed: unknown = JSON.parse(preview.previewJson);
  if (preview.kind === StepValuePreviewKindV2.JSON) {
    return {
      kind: "json",
      typeName: preview.typeName,
      value: parseJsonValue(parsed, path),
      truncated: preview.truncated,
    };
  }
  if (preview.kind === StepValuePreviewKindV2.REPR) {
    if (typeof parsed !== "string") throw new Error(`${path} repr preview must be a string`);
    return {
      kind: "repr",
      typeName: preview.typeName,
      text: parsed,
      truncated: preview.truncated,
    };
  }
  throw new Error(`${path} has an unknown preview kind ${preview.kind}`);
}

/** Validate a wire preview once; malformed previews are a server contract violation. */
export function mapStepIo(stepIo: StepIoV2): StepIo {
  const mapped: StepIo = {
    inputs: stepIo.inputs.map((parameter) => {
      if (!parameter.value) throw new Error(`Step input ${parameter.name} omitted its value`);
      return {
        name: parameter.name,
        value: mapStepValuePreview(parameter.value, `inputs.${parameter.name}`),
      };
    }),
  };
  if (stepIo.output) mapped.output = mapStepValuePreview(stepIo.output, "output");
  return mapped;
}
