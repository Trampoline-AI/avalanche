import { describe, expect, it } from "vitest";

import { FlowInfoMsg } from "./model";
import { workflowInputDraft } from "./workflowInput";

const schema = JSON.stringify({
  type: "object",
  properties: {
    region: { type: "string", default: "eu" },
    limit: { type: "integer" },
    verbose: { type: "boolean" },
  },
  required: ["limit"],
});

describe("workflowInputDraft", () => {
  it("starts from declared defaults and marks required fields without one", () => {
    expect(workflowInputDraft(FlowInfoMsg.create({ inputSchemaJson: schema }))).toEqual({
      region: "eu",
      limit: null,
    });
  });

  it("prefers the input the schedule already runs with", () => {
    const flow = FlowInfoMsg.create({
      inputSchemaJson: schema,
      scheduleInput: { limit: 5, verbose: true },
    });
    expect(workflowInputDraft(flow)).toEqual({ region: "eu", limit: 5, verbose: true });
  });

  it.each([
    ["no schema", ""],
    ["free-form input", JSON.stringify({ type: "object", additionalProperties: true })],
    ["an empty input model", JSON.stringify({ type: "object", properties: {} })],
  ])("asks for no input for %s", (_name, inputSchemaJson) => {
    expect(workflowInputDraft(FlowInfoMsg.create({ inputSchemaJson }))).toBeUndefined();
  });
});
