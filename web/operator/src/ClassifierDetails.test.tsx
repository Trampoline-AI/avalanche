import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  decodeClassifierDeclaration,
  decodeClassifierInvocation,
  parseClassifierDeclaration,
  parseClassifierInvocation,
  type ClassificationResult,
  type ClassifierDeclaration,
  type ClassifierInvocation,
} from "./classifier";
import {
  ClassifierDefinition,
  ClassifierInvocationDetails,
  ClassifierQuestions,
} from "./ClassifierDetails";

const declaration: ClassifierDeclaration = {
  runtime: { model: "jev-latest", timeout: 10 },
  input_schema: null,
  step_inputs: [],
  step_output: { type_name: "Unspecified", json_schema: null },
  questions: {
    route: {
      type: "choice",
      instructions: {
        kind: "predict_rlm_file",
        path: "/an/authored/question",
        task: "Route the request",
        constraints: ["Use the current policy", { version: 4 }],
      },
      criteria: {
        review: {
          kind: "unavailable",
          reason: "An authored rubric",
          policy: "Needs a reviewer",
          exclusions: ["Already approved"],
        },
        accept: null,
      },
    },
    safe: {
      type: "noul",
      instructions: "Does the policy permit this request?",
      criteria: {
        true: { evidence: "Permission is recorded" },
        false: ["Permission is missing"],
      },
    },
    urgency: {
      type: "score",
      instructions: ["Judge the impact", { scope: "Current request" }],
      criteria: [
        { definition: "Can wait", hours: 24 },
        { definition: "Needs action now", hours: 0 },
      ],
    },
  },
};

function success(): ClassifierInvocation & { result: ClassificationResult } {
  return {
    invocation_id: "call-first",
    invocation_index: 0,
    status: "success",
    started_at: 100,
    ended_at: 101.5,
    declaration,
    input: { request: "First request", context: { policy: ["Policy one"] } },
    error: null,
    result: {
      model: "jev-1.13",
      usage: { input_tokens: 321, output_tokens: 47 },
      answers: {
        route: {
          type: "choice",
          choice: "review",
          probabilities: { review: 0.72, accept: 0.28 },
          confidence: 0.44,
        },
        safe: { type: "noul", noul: 0.91 },
        urgency: {
          type: "score",
          score: 0.375,
          confidence: 0.25,
          probabilities: { "0": 0.625, "1": 0.375 },
          legend: {
            "0": { hours: 24, definition: "Can wait" },
            "1": { hours: 0, definition: "Needs action now" },
          },
        },
      },
    },
  };
}

it("retains structured instructions and criteria under their independent question IDs", () => {
  const decoded = decodeClassifierDeclaration(JSON.stringify(declaration));
  render(<ClassifierQuestions declaration={decoded} />);
  const route = within(screen.getByRole("region", { name: "Question route" }));
  expect(
    screen
      .getAllByRole("region", { name: /^Question / })
      .map((region) => region.getAttribute("aria-label")),
  ).toEqual(["Question route", "Question safe", "Question urgency"]);
  expect(route.getByText(/Route the request/)).toBeInTheDocument();
  fireEvent.click(route.getByRole("button", { name: "Expand constraints" }));
  expect(route.getByText(/Use the current policy/)).toBeInTheDocument();
  fireEvent.click(route.getByRole("button", { expanded: false, name: /criteria/ }));
  expect(route.getByRole("list", { name: "route options" })).toBeInTheDocument();
  expect(route.getByRole("heading", { name: "review" })).toBeInTheDocument();
  expect(route.getByText(/Needs a reviewer/)).toBeInTheDocument();
  fireEvent.click(route.getByRole("button", { name: "Expand exclusions" }));
  expect(route.getByText(/Already approved/)).toBeInTheDocument();
  const safe = within(screen.getByRole("region", { name: "Question safe" }));
  fireEvent.click(safe.getByRole("button", { expanded: false }));
  expect(safe.getByRole("group", { name: "safe true criterion" })).toHaveTextContent("True");
  expect(safe.getByRole("group", { name: "safe false criterion" })).toHaveTextContent("False");
  expect(safe.getByText(/Permission is recorded/)).toBeInTheDocument();
  expect(safe.queryByText(/Needs a reviewer/)).not.toBeInTheDocument();
  const urgency = within(screen.getByRole("region", { name: "Question urgency" }));
  expect(urgency.getByText(/Judge the impact/)).toBeInTheDocument();
  fireEvent.click(urgency.getByRole("button", { expanded: false, name: /criteria/ }));
  expect(urgency.getByRole("list", { name: "urgency ordered levels" })).toHaveAttribute(
    "start",
    "0",
  );
  expect(urgency.getByText(/Can wait/)).toBeInTheDocument();
});

it("exposes nested serialized fields, nullable variants, constraints, and finite recursive references", () => {
  const decoded = parseClassifierDeclaration({
    ...declaration,
    step_inputs: [
      {
        name: "batch",
        type_name: "list[str]",
        json_schema: { type: "array", items: { type: "string" } },
        required: true,
      },
    ],
    step_output: { type_name: "bool", json_schema: { type: "boolean" } },
    input_schema: {
      title: "FollowupInput",
      type: "object",
      properties: { item: { $ref: "#/$defs/Follow~1up" } },
      required: ["item"],
      $defs: {
        "Follow/up": {
          type: "object",
          properties: {
            text: { type: "string", description: "Action to complete", minLength: 2 },
            owner: { anyOf: [{ type: "string" }, { type: "null" }], default: null },
            children: { type: "array", items: { $ref: "#/$defs/Follow~1up" }, default: [] },
          },
          required: ["text"],
        },
      },
    },
  });
  render(<ClassifierDefinition declaration={decoded} />);
  const inputs = within(screen.getByRole("region", { name: "Step inputs" }));
  expect(inputs.getByText("batch")).toBeVisible();
  expect(inputs.queryByText("item")).toBeNull();
  expect(screen.getByRole("region", { name: "Step output" })).toHaveTextContent("bool");
  const state = within(screen.getByRole("region", { name: "Classifier input" }));
  expect(state.queryByText("text")).toBeNull();
  fireEvent.click(state.getByRole("button", { name: "Expand state schema" }));
  fireEvent.click(state.getByRole("button", { name: "Expand item schema" }));
  expect(state.getByText("text")).toBeVisible();
  expect(state.getByText("owner")).toBeVisible();
  expect(
    within(state.getByRole("group", { name: "text schema" })).getByText("Required"),
  ).toBeVisible();
  expect(
    within(state.getByRole("group", { name: "owner schema" })).getByText("Optional"),
  ).toBeVisible();
  fireEvent.click(state.getByRole("button", { name: "Expand text schema" }));
  expect(state.getByText("Action to complete")).toBeVisible();
  expect(state.getByRole("group", { name: "text minLength" })).toHaveTextContent("2");
  fireEvent.click(state.getByRole("button", { name: "Expand owner schema" }));
  expect(state.getByRole("group", { name: "owner default" })).toHaveTextContent("null");
  expect(state.getByRole("group", { name: "anyOf 1 schema" })).toHaveTextContent("string");
  expect(state.getByRole("group", { name: "anyOf 2 schema" })).toHaveTextContent("null");
  fireEvent.click(state.getByRole("button", { name: "Expand children schema" }));
  fireEvent.click(state.getByRole("button", { name: "Expand Items schema" }));
  expect(state.getByText(/Recursive reference/)).toBeVisible();
});

it("distinguishes empty signatures, missing annotations, unsupported annotations, and undeclared state models", () => {
  const view = render(<ClassifierDefinition declaration={declaration} />);
  const inputs = screen.getByRole("region", { name: "Step inputs" });
  expect(within(inputs).queryByText("Unspecified")).toBeNull();
  expect(screen.getByRole("region", { name: "Step output" })).toHaveTextContent("Unspecified");
  expect(
    within(screen.getByRole("region", { name: "Classifier input" })).queryByRole("button"),
  ).toBeNull();
  view.rerender(
    <ClassifierDefinition
      declaration={{
        ...declaration,
        step_inputs: [
          { name: "context", type_name: "Unspecified", json_schema: null, required: true },
          { name: "connection", type_name: "Connection", json_schema: null, required: false },
        ],
        input_schema: {},
      }}
    />,
  );
  expect(inputs).toHaveTextContent("context");
  expect(inputs).toHaveTextContent("Unspecified");
  expect(inputs).toHaveTextContent("connection");
  expect(inputs).toHaveTextContent("Connection");
  expect(within(inputs).getByText("Required")).toBeVisible();
  expect(within(inputs).getByText("Optional")).toBeVisible();
  expect(screen.getByRole("region", { name: "Classifier input" })).toHaveTextContent(
    "Any JSON",
  );
});

it("shows unresolved and external schema references without following them", () => {
  render(
    <ClassifierDefinition
      declaration={{
        ...declaration,
        input_schema: {
          type: "object",
          properties: {
            missing: { $ref: "#/$defs/Missing" },
            remote: { $ref: "https://schemas.example.test/Remote" },
          },
        },
      }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Expand state schema" }));
  fireEvent.click(screen.getByRole("button", { name: "Expand missing schema" }));
  fireEvent.click(screen.getByRole("button", { name: "Expand remote schema" }));
  expect(screen.getByText(/Unresolved local reference/)).toBeVisible();
  expect(screen.getByText(/External reference; not fetched/)).toBeVisible();
});

it("distinguishes an omitted Noul criterion from an explicitly null criterion", () => {
  render(
    <ClassifierQuestions
      declaration={{
        ...declaration,
        questions: {
          permitted: {
            type: "noul",
            instructions: "Can this proceed?",
            criteria: { true: null },
          },
        },
      }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { expanded: false }));
  const trueCriterion = within(screen.getByRole("group", { name: "permitted true criterion" }));
  const falseCriterion = within(
    screen.getByRole("group", { name: "permitted false criterion" }),
  );
  expect(trueCriterion.getByText("null")).toBeInTheDocument();
  expect(falseCriterion.queryByText("null")).not.toBeInTheDocument();
  expect(falseCriterion.getByRole("definition")).toBeEmptyDOMElement();
});

it("renders typed answers without turning Noul into confidence or rounding Score to a level", () => {
  render(
    <ClassifierInvocationDetails
      invocation={decodeClassifierInvocation(JSON.stringify(success()))}
    />,
  );
  const route = within(screen.getByRole("region", { name: "Answer route" }));
  expect(route.getByText("review", { selector: "span" })).toBeVisible();
  expect(route.queryByText(/Route the request/)).not.toBeInTheDocument();
  expect(route.queryByRole("button")).not.toBeInTheDocument();
  expect(route.queryByText(/Needs a reviewer/)).not.toBeInTheDocument();
  const safe = within(screen.getByRole("region", { name: "Answer safe" }));
  expect(safe.getByRole("meter")).toHaveAttribute("aria-valuenow", "0.91");
  expect(safe.queryByText(/Confidence:/)).not.toBeInTheDocument();
  expect(safe.queryByText(/Does the policy permit/)).not.toBeInTheDocument();
  expect(safe.queryByRole("button")).not.toBeInTheDocument();
  const urgency = within(screen.getByRole("region", { name: "Answer urgency" }));
  expect(urgency.getByText("0.375", { selector: "span" })).toBeVisible();
  expect(urgency.queryByText(/Can wait|Needs action now/)).not.toBeInTheDocument();
  expect(urgency.getByText(/25%/)).toBeVisible();
  fireEvent.click(urgency.getByRole("button", { expanded: false, name: /criteria/ }));
  expect(urgency.getByRole("heading", { name: "0" })).toBeVisible();
  expect(urgency.getByRole("heading", { name: "1" })).toBeVisible();
  expect(
    urgency.queryByText(/Can wait|Needs action now|Judge the impact/),
  ).not.toBeInTheDocument();
});

it("pairs each repeated call's retained input with its own answers", () => {
  const first = success();
  const second = success();
  second.invocation_id = "call-second";
  second.invocation_index = 1;
  second.input = { request: "Second request", context: { policy: ["Policy two"] } };
  second.result.answers.route = {
    type: "choice",
    choice: "accept",
    probabilities: { review: 0.12, accept: 0.88 },
    confidence: 0.76,
  };
  render(
    <>
      <ClassifierInvocationDetails invocation={first} />
      <ClassifierInvocationDetails invocation={second} />
    </>,
  );
  const firstCall = within(
    screen.getByRole("region", { name: "Classifier invocation call-first" }),
  );
  const secondCall = within(
    screen.getByRole("region", { name: "Classifier invocation call-second" }),
  );
  const firstInput = within(firstCall.getByRole("region", { name: "Input state" }));
  const secondInput = within(secondCall.getByRole("region", { name: "Input state" }));
  expect(firstInput.getByText(/First request/)).toBeInTheDocument();
  expect(firstInput.queryByText(/Second request/)).not.toBeInTheDocument();
  expect(secondInput.getByText(/Second request/)).toBeInTheDocument();
  expect(firstInput.getByText(/Policy one/)).toBeInTheDocument();
  expect(firstInput.getByRole("textbox")).toHaveAttribute("aria-readonly", "true");
  expect(secondInput.queryByText(/Policy one/)).not.toBeInTheDocument();
  expect(firstCall.getByRole("region", { name: "Answer route" })).toHaveTextContent("review");
  expect(secondCall.getByRole("region", { name: "Answer route" })).toHaveTextContent("accept");
});

it("transitions from running to a failed record without fabricating answers or interpreting error HTML", () => {
  const running: ClassifierInvocation = {
    ...success(),
    status: "running",
    ended_at: null,
    result: null,
  };
  const view = render(
    <ClassifierInvocationDetails invocation={parseClassifierInvocation(running)} />,
  );
  expect(screen.getByRole("status")).toBeInTheDocument();
  expect(
    within(screen.getByRole("region", { name: "Input state" })).getByText(/First request/),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("group", { name: "Classification answers" }),
  ).not.toBeInTheDocument();
  const error =
    'Classifier request failed: <img src="https://tracker.invalid" onerror="alert(1)">';
  view.rerender(
    <ClassifierInvocationDetails
      invocation={parseClassifierInvocation({
        ...running,
        status: "failed",
        ended_at: 102,
        error,
      })}
    />,
  );
  expect(screen.getByRole("alert")).toHaveTextContent(error);
  expect(
    within(screen.getByRole("region", { name: "Input state" })).getByText(/First request/),
  ).toBeInTheDocument();
  expect(view.container.querySelector("img")).toBeNull();
  expect(
    screen.queryByRole("group", { name: "Classification answers" }),
  ).not.toBeInTheDocument();
  view.rerender(
    <ClassifierInvocationDetails
      invocation={parseClassifierInvocation({ ...running, status: "cancelled", ended_at: 102 })}
    />,
  );
  expect(screen.getByRole("status")).toHaveTextContent(/cancelled/i);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByRole("meter")).not.toBeInTheDocument();
});

it("keeps valid empty inputs distinct from state that failed validation before capture", () => {
  const invocation = success();
  const view = render(
    <ClassifierInvocationDetails invocation={{ ...invocation, input: "" }} />,
  );
  expect(
    within(screen.getByRole("region", { name: "Input state" })).getByRole("textbox"),
  ).toHaveTextContent('""');
  view.rerender(<ClassifierInvocationDetails invocation={{ ...invocation, input: [] }} />);
  expect(
    within(screen.getByRole("region", { name: "Input state" })).getByRole("textbox"),
  ).toHaveTextContent("[]");
  view.rerender(<ClassifierInvocationDetails invocation={{ ...invocation, input: {} }} />);
  expect(
    within(screen.getByRole("region", { name: "Input state" })).getByRole("textbox"),
  ).toHaveTextContent("{}");
  view.rerender(
    <ClassifierInvocationDetails
      invocation={{
        ...invocation,
        input: null,
        result: null,
        status: "failed",
        error: "Invalid classifier state",
      }}
    />,
  );
  const input = within(screen.getByRole("region", { name: "Input state" }));
  expect(input.queryByRole("textbox")).not.toBeInTheDocument();
  expect(input.getByText(/not captured/i)).toBeInTheDocument();
});

it("shows the highest probabilities first and reveals every rounded option on expansion", () => {
  const value = success();
  value.declaration = {
    ...declaration,
    questions: {
      route: {
        type: "choice",
        instructions: "What kind of follow-up is this?",
        criteria: { other: null, proposal: null, request: null, problem: null },
      },
    },
  };
  value.result.answers = {
    route: {
      type: "choice",
      choice: "problem",
      probabilities: { other: 0.01, proposal: 0.02, request: 0.4, problem: 0.56 },
      confidence: 0.36,
    },
  };
  render(<ClassifierInvocationDetails invocation={parseClassifierInvocation(value)} />);
  const answer = within(screen.getByRole("region", { name: "Answer route" }));
  expect(answer.getAllByRole("listitem").map((item) => item.textContent)).toEqual([
    "problem56%",
    "request40%",
    "proposal2%",
  ]);
  expect(answer.queryByText("other")).not.toBeInTheDocument();
  fireEvent.click(answer.getByRole("button", { expanded: false }));
  expect(answer.getAllByRole("listitem").map((item) => item.textContent)).toEqual([
    "problem56%",
    "request40%",
    "proposal2%",
    "other1%",
  ]);
  fireEvent.click(answer.getByRole("button", { expanded: true }));
  expect(answer.queryByText("other")).not.toBeInTheDocument();
});

describe("strict classifier records", () => {
  it.each(["input_schema", "step_inputs", "step_output"])(
    "requires declaration metadata %s at the JSON boundary",
    (key) => {
      expect(() =>
        decodeClassifierDeclaration(JSON.stringify({ ...declaration, [key]: undefined })),
      ).toThrow();
    },
  );

  it.each([
    { input_schema: [] },
    { input_schema: false },
    { step_inputs: {} },
    { step_inputs: [{ name: "x", type_name: "str", json_schema: null, required: "yes" }] },
    {
      step_inputs: [
        { name: "x", type_name: "str", json_schema: null, required: true, unexpected: true },
      ],
    },
    { step_output: { type_name: "str" } },
    { step_output: { type_name: "str", json_schema: [], unexpected: true } },
  ])("rejects malformed signature metadata: %j", (metadata) => {
    expect(() => parseClassifierDeclaration({ ...declaration, ...metadata })).toThrow();
  });

  it.each([
    { properties: { nested: { type: "python" } } },
    { $defs: { Model: { required: [true] } } },
    { anyOf: [{ type: "string" }, null] },
    { items: { minLength: -1 } },
    { additionalProperties: null },
    { description: 42 },
    { enum: "one" },
    { default: { value: Infinity } },
    { "x-extra": { value: undefined } },
  ])(
    "rejects malformed nested schemas without losing their declaration: %j",
    (input_schema) => {
      expect(() => parseClassifierDeclaration({ ...declaration, input_schema })).toThrow();
    },
  );

  it("preserves JSON schema extension keywords and prototype-like property names", () => {
    const input_schema = {
      type: "object",
      properties: { constructor: { type: "string" } },
      additionalProperties: false,
      "x-policy": { versions: [1, null], enabled: true },
    };
    const serialized = JSON.stringify({ ...declaration, input_schema }).replace(
      '"x-policy":',
      '"__proto__":{"type":"number"},"x-policy":',
    );
    const decoded = decodeClassifierDeclaration(serialized);
    expect(decoded.input_schema).toMatchObject(input_schema);
    expect(Object.hasOwn(decoded.input_schema ?? {}, "__proto__")).toBe(true);
    expect(decoded.input_schema?.type).toBe("object");
    expect(decoded.input_schema?.["__proto__"]).toEqual({ type: "number" });
  });

  it("requires a JSON state entry, rejects scalar state, and preserves nested JSON", () => {
    const value = success();
    const { input, ...withoutInput } = value;
    expect(() => parseClassifierInvocation(withoutInput)).toThrow();
    expect(() => parseClassifierInvocation({ ...value, input: false })).toThrow();
    expect(() => parseClassifierInvocation({ ...value, input: 42 })).toThrow();
    expect(parseClassifierInvocation(value).input).toEqual(input);
    expect(
      parseClassifierInvocation({ ...value, input: { enabled: false, attempts: 42 } }).input,
    ).toEqual({ enabled: false, attempts: 42 });
    expect(() => parseClassifierInvocation({ ...value, input: { score: Infinity } })).toThrow();
  });

  it("rejects partial answers rather than presenting a completed classification", () => {
    const value = success();
    delete value.result.answers.safe;
    expect(() => parseClassifierInvocation(value)).toThrow();
  });

  it("renders a valid single-option Choice", () => {
    const value = success();
    const parsed = parseClassifierInvocation({
      ...value,
      declaration: {
        ...declaration,
        questions: { only: { type: "choice", instructions: null, criteria: { retain: null } } },
      },
      result: {
        model: "jev-1.13",
        usage: { input_tokens: 10, output_tokens: 1 },
        answers: {
          only: {
            type: "choice",
            choice: "retain",
            probabilities: { retain: 1 },
            confidence: 1,
          },
        },
      },
    });
    render(<ClassifierInvocationDetails invocation={parsed} />);
    expect(screen.getByRole("region", { name: "Answer only" })).toHaveTextContent("retain");
  });

  it.each([
    { type: "choice", choice: "review", probabilities: { review: 1 }, confidence: 1 },
    {
      type: "choice",
      choice: "unknown",
      probabilities: { review: 0.72, accept: 0.28 },
      confidence: 0.44,
    },
    {
      type: "choice",
      choice: "review",
      probabilities: { review: 0.4, accept: 0.3 },
      confidence: 0.44,
    },
    {
      type: "choice",
      choice: "accept",
      probabilities: { review: 0.72, accept: 0.28 },
      confidence: 0.44,
    },
    { type: "noul", noul: 0.72 },
  ])("rejects an answer inconsistent with its declared Choice: %j", (answer) => {
    const value = success();
    expect(() =>
      parseClassifierInvocation({
        ...value,
        result: { ...value.result, answers: { ...value.result.answers, route: answer } },
      }),
    ).toThrow();
  });

  it("rejects confidence on Noul and out-of-range probability", () => {
    const value = success();
    const answers = value.result.answers;
    expect(() =>
      parseClassifierInvocation({
        ...value,
        result: {
          ...value.result,
          answers: { ...answers, safe: { type: "noul", noul: 0.5, confidence: 0.8 } },
        },
      }),
    ).toThrow();
    expect(() =>
      parseClassifierInvocation({
        ...value,
        result: {
          ...value.result,
          answers: { ...answers, safe: { type: "noul", noul: 1.01 } },
        },
      }),
    ).toThrow();
  });

  it("rejects a Score legend that no longer describes the historical criteria", () => {
    const value = success();
    const answer = value.result.answers.urgency;
    expect(() =>
      parseClassifierInvocation({
        ...value,
        result: {
          ...value.result,
          answers: {
            ...value.result.answers,
            urgency: { ...answer, legend: { "0": "New definition", "1": "Urgent" } },
          },
        },
      }),
    ).toThrow();
  });

  it("rejects a Score inconsistent with its distribution", () => {
    const value = success();
    const answer = value.result.answers.urgency;
    expect(() =>
      parseClassifierInvocation({
        ...value,
        result: {
          ...value.result,
          answers: { ...value.result.answers, urgency: { ...answer, score: 1 } },
        },
      }),
    ).toThrow();
  });

  it("rejects incomplete lifecycle records and unexpected fields", () => {
    const value = success();
    expect(() => parseClassifierInvocation({ ...value, result: null })).toThrow();
    expect(() => parseClassifierInvocation({ ...value, status: "running" })).toThrow();
    expect(() => parseClassifierInvocation({ ...value, ended_at: 99 })).toThrow();
    expect(() =>
      parseClassifierInvocation({
        ...value,
        state: { request: "Use the required input field" },
      }),
    ).toThrow();
  });

  it("rejects malformed declarations and JSON instead of omitting them", () => {
    expect(() => decodeClassifierDeclaration("{broken")).toThrow();
    expect(() => decodeClassifierInvocation("null")).toThrow();
    expect(() =>
      parseClassifierDeclaration({
        ...declaration,
        questions: { bad: { type: "score", instructions: "Rank", criteria: ["Low", null] } },
      }),
    ).toThrow();
    expect(() =>
      parseClassifierDeclaration({
        ...declaration,
        questions: { bad: { type: "noul", instructions: true, criteria: null } },
      }),
    ).toThrow();
  });
});
