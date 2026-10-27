import { describe, expect, it } from "vitest";

import { parseAgentDeclaration } from "./GraphCanvas";
import { AgentLiveEvent, AgentTraceTerminal } from "./generated/agentSchemas";
import { finishedTrace, runStartedEvent, turnEvent } from "./test/agentTraceFixtures";
import { agentDeclarationJson } from "./test/fixtures";

const finished = finishedTrace();
if (finished.kind !== "trace_finished") throw new Error("fixture is a finished trace");
const { header } = finished;

describe("generated agent schemas", () => {
  it.each([
    [
      "a stored runtime kind",
      { ...finished, header: { ...header, runtime_kind: "predict_rlm" } },
    ],
    ["a stored trace format", { ...finished, header: { ...header, trace_format: "x/v1" } }],
    ["a trace record", { ...finished, record: { header, turns: [] } }],
    ["an unknown field", { ...finished, extra: true }],
    ["an old schema version", { ...finished, schema_version: 1 }],
    ["a negative duration", { ...finished, header: { ...header, duration_ms: -1 } }],
    [
      "an unknown header format",
      { ...finished, header: { ...header, native: { format: "other/v1" } } },
    ],
    ["outputs that are not an object", { ...finished, outputs: ["done"] }],
  ])("reject a final trace with %s", (_case, body) => {
    expect(AgentTraceTerminal.safeParse(body).success).toBe(false);
  });

  it("accept live event and final trace bodies shaped like the Python models", () => {
    expect(AgentLiveEvent.parse(turnEvent(1))).toEqual(turnEvent(1));
    expect(AgentLiveEvent.parse(runStartedEvent({ a: 1 }))).toEqual(runStartedEvent({ a: 1 }));
    expect(AgentTraceTerminal.parse(finishedTrace())).toEqual(finishedTrace());
  });

  it.each([
    ["a v1 body", { event_kind: "iteration.recorded", data: {} }],
    ["an old schema version", { ...turnEvent(1), schema_version: 1 }],
    ["a stored runtime kind", { ...turnEvent(1), runtime_kind: "predict_rlm" }],
    [
      "an unknown native format",
      { ...turnEvent(1), native: { format: "other/v1", kind: "code.generated" } },
    ],
    [
      "an unknown native kind",
      { ...turnEvent(1), native: { format: "predict_rlm.run/v1", kind: "code.compiled" } },
    ],
    ["an unknown common kind", { ...turnEvent(1), common: { kind: "turn.paused" } }],
  ])("reject a live event with %s", (_case, body) => {
    expect(AgentLiveEvent.safeParse(body).success).toBe(false);
  });
});

describe("parseAgentDeclaration", () => {
  it("reads a published declaration", () => {
    const declaration = parseAgentDeclaration(
      agentDeclarationJson({
        instructions: "Answer.",
        inputs: [{ name: "question", annotation: "str" }],
        runtime: { settings: { max_iterations: 3 } },
      }),
    );
    expect(declaration?.inputs).toEqual([{ name: "question", type: "str", description: "" }]);
    expect(declaration?.runtime).toEqual({ max_iterations: 3 });
  });

  it("reads a Claude Agent SDK declaration", () => {
    const declaration = parseAgentDeclaration(
      JSON.stringify({
        signature: { name: "Sum", instructions: "Add.", inputs: [], outputs: [] },
        tools: [{ name: "lookup", description: "", source_code: "def lookup(): ..." }],
        skills: [
          {
            name: "house-style",
            description: "Applies the house style.",
            instructions: "Be brief.",
            files: ["references/style.md"],
          },
        ],
        runtime: {
          kind: "claude_agent",
          model: "claude-haiku-5-5",
          settings: { max_turns: 4 },
          builtin_tools: ["Read"],
        },
      }),
    );
    expect(declaration?.runtime).toEqual({ max_turns: 4, builtin_tools: ["Read"] });
    expect(declaration?.skills).toEqual([
      {
        name: "house-style",
        description: "Applies the house style.",
        instructions: "Be brief.",
        packages: [],
        modules: [],
        files: ["references/style.md"],
      },
    ]);
    expect(declaration?.tools.map((tool) => tool.name)).toEqual(["lookup"]);
  });

  it("lists the step's Agent Skills before PredictRLM's own", () => {
    const declaration = parseAgentDeclaration(
      agentDeclarationJson({
        skills: [{ name: "house-style", description: "d", instructions: "", files: [] }],
        runtime: {
          skills: [{ name: "pdf", instructions: "", packages: [], modules: [], tools: [] }],
        },
      }),
    );
    expect(declaration?.skills.map((skill) => skill.name)).toEqual(["house-style", "pdf"]);
  });

  it("throws on a malformed declaration instead of dropping fields", () => {
    const declaration = JSON.parse(agentDeclarationJson({ instructions: "Answer." }));
    delete declaration.runtime.kind;
    expect(() => parseAgentDeclaration(JSON.stringify(declaration))).toThrow();
  });
});
