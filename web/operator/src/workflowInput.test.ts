import { describe, expect, it } from "vitest";

import { FlowInfoMsg } from "./model";
import {
  COMPUTED_DEFAULT_PLACEHOLDER,
  defaultPlaceholder,
  scheduledInputNote,
  workflowInputDraft,
  type FieldDefault,
  type WorkflowInputField,
} from "./workflowInput";

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
  it("starts empty, leaving every field to its default", () => {
    expect(workflowInputDraft(FlowInfoMsg.create({ inputSchemaJson: schema }))).toEqual({});
  });

  it("holds only the input the schedule already runs with", () => {
    const flow = FlowInfoMsg.create({
      inputSchemaJson: schema,
      scheduleInput: { limit: 5, verbose: true },
    });
    expect(workflowInputDraft(flow)).toEqual({ limit: 5, verbose: true });
  });

  it.each([
    ["no schema", ""],
    ["free-form input", JSON.stringify({ type: "object", additionalProperties: true })],
    ["an empty input model", JSON.stringify({ type: "object", properties: {} })],
  ])("asks for no input for %s", (_name, inputSchemaJson) => {
    expect(workflowInputDraft(FlowInfoMsg.create({ inputSchemaJson }))).toBeUndefined();
  });
});

describe("scheduledInputNote", () => {
  it("says what a workflow's scheduled runs start with", () => {
    const cron = "0 6 * * *";
    expect(scheduledInputNote(FlowInfoMsg.create({}))).toBeUndefined();
    expect(scheduledInputNote(FlowInfoMsg.create({ cron }))).toMatch(/off/);
    expect(scheduledInputNote(FlowInfoMsg.create({ cron, scheduleEnabled: true }))).toMatch(
      /defaults/,
    );
    expect(
      scheduledInputNote(
        FlowInfoMsg.create({ cron, scheduleEnabled: true, scheduleInput: { limit: 3 } }),
      ),
    ).toMatch(/schedule's input/);
  });
});

describe("defaultPlaceholder", () => {
  const field = (
    defaultValue: FieldDefault,
    schema: WorkflowInputField["schema"],
  ): WorkflowInputField => ({ name: "field", typeLabel: "", default: defaultValue, schema });

  it.each<[string, WorkflowInputField, string]>([
    [
      "a text default as typed",
      field({ kind: "value", value: "eu" }, { type: "string" }),
      "eu",
    ],
    [
      "a choice default as typed",
      field({ kind: "value", value: "USD" }, { enum: ["USD", "EUR"] }),
      "USD",
    ],
    [
      "a list default as JSON",
      field({ kind: "value", value: ["west", "east"] }, { type: "array" }),
      '["west","east"]',
    ],
    [
      "a bool default as JSON",
      field({ kind: "value", value: false }, { type: "boolean" }),
      "false",
    ],
    [
      "a null default as JSON",
      field({ kind: "value", value: null }, { anyOf: [{ type: "string" }, { type: "null" }] }),
      "null",
    ],
    [
      "a computed default",
      field({ kind: "computed" }, { type: "string" }),
      COMPUTED_DEFAULT_PLACEHOLDER,
    ],
    ["nothing for a required field", field({ kind: "required" }, { type: "string" }), ""],
  ])("shows %s", (_case, input, expected) => {
    expect(defaultPlaceholder(input)).toBe(expected);
  });
});
