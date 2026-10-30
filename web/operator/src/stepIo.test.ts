import { describe, expect, it } from "vitest";

import { StepValuePreviewKindV2 } from "./generated/operator";
import { mapStepIo } from "./stepIo";

function preview(kind: StepValuePreviewKindV2, value: unknown, truncated = false) {
  return { kind, typeName: "dict", previewJson: JSON.stringify(value), truncated };
}

describe("step value previews", () => {
  it("decodes JSON and repr previews once at the API boundary", () => {
    expect(
      mapStepIo({
        inputs: [
          {
            name: "batch",
            value: preview(
              StepValuePreviewKindV2.JSON,
              { rows: [1, "… [+9 more items]"] },
              true,
            ),
          },
          { name: "frame", value: preview(StepValuePreviewKindV2.REPR, "shape: (2, 1)") },
        ],
        output: preview(StepValuePreviewKindV2.JSON, null),
      }),
    ).toEqual({
      inputs: [
        {
          name: "batch",
          value: {
            kind: "json",
            typeName: "dict",
            value: { rows: [1, "… [+9 more items]"] },
            truncated: true,
          },
        },
        {
          name: "frame",
          value: { kind: "repr", typeName: "dict", text: "shape: (2, 1)", truncated: false },
        },
      ],
      output: { kind: "json", typeName: "dict", value: null, truncated: false },
    });
  });

  it("keeps an absent output distinct from a returned None", () => {
    expect(mapStepIo({ inputs: [] })).toEqual({ inputs: [] });
  });

  it.each([
    ["an unknown kind", preview(StepValuePreviewKindV2.UNSPECIFIED, 1)],
    ["a repr that is not text", preview(StepValuePreviewKindV2.REPR, { text: "x" })],
    ["invalid JSON", { ...preview(StepValuePreviewKindV2.JSON, 1), previewJson: "{" }],
  ])("rejects %s", (_label, output) => {
    expect(() => mapStepIo({ inputs: [], output })).toThrow();
  });

  it("rejects an input without a value", () => {
    expect(() => mapStepIo({ inputs: [{ name: "batch" }] })).toThrow(/batch/);
  });
});
