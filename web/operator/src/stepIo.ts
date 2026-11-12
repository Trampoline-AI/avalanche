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

/** Which accepted result to read: a run's, a machine state entry's, or one step's. */
export interface RunResultTarget {
  /** A machine state entry; with `nodeId`, that state entry's step (unqualified, e.g. `triage_1`). */
  entry?: number;
  nodeId?: string;
}

/**
 * The display form of a shared run result (`{"version": 1, "value": ...}`, the
 * codec every host uses): scalars, lists and dicts become plain JSON, a tuple a
 * list, and a File or Workspace its immutable reference object. Never bytes.
 */
export function decodeRunResultValue(encoded: unknown, path = "result"): JsonValue {
  if (typeof encoded !== "object" || encoded === null || Array.isArray(encoded))
    throw new Error(`${path} is not an encoded result value`);
  const value = encoded as Record<string, unknown>;
  switch (value.kind) {
    case "scalar":
      return parseJsonValue(value.value, path);
    case "list":
    case "tuple":
      if (!Array.isArray(value.items)) throw new Error(`${path} has no items`);
      return value.items.map((item, index) => decodeRunResultValue(item, `${path}[${index}]`));
    case "dict": {
      if (!Array.isArray(value.items)) throw new Error(`${path} has no items`);
      const result: Record<string, JsonValue> = {};
      for (const item of value.items as unknown[]) {
        if (!Array.isArray(item) || item.length !== 2 || typeof item[0] !== "string")
          throw new Error(`${path} has a malformed item`);
        result[item[0]] = decodeRunResultValue(item[1], `${path}.${item[0]}`);
      }
      return result;
    }
    case "file":
    case "workspace":
      return parseJsonValue(value, path);
    default:
      throw new Error(`${path} has an unknown result kind ${String(value.kind)}`);
  }
}

/** Parse a host's result document (`{"version": 1, "value": ...}`). */
export function parseRunResultDocument(valueJson: string): JsonValue {
  const document: unknown = JSON.parse(valueJson);
  if (
    typeof document !== "object" ||
    document === null ||
    (document as Record<string, unknown>).version !== 1
  )
    throw new Error("Unsupported run result document");
  return decodeRunResultValue((document as Record<string, unknown>).value);
}
