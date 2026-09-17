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
import { ClassifierInvocationDetails, ClassifierQuestions } from "./ClassifierDetails";

const declaration: ClassifierDeclaration = {
  runtime: { model: "jev-latest", timeout: 10 },
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
  expect(route.getByRole("list", { name: "route options" })).toBeInTheDocument();
  expect(route.getByRole("heading", { name: "review" })).toBeInTheDocument();
  expect(route.getByText(/Needs a reviewer/)).toBeInTheDocument();
  fireEvent.click(route.getByRole("button", { name: "Expand exclusions" }));
  expect(route.getByText(/Already approved/)).toBeInTheDocument();
  const safe = within(screen.getByRole("region", { name: "Question safe" }));
  expect(safe.getByRole("group", { name: "safe yes criterion" })).toHaveTextContent("true");
  expect(safe.getByText(/Permission is recorded/)).toBeInTheDocument();
  expect(safe.queryByText(/Needs a reviewer/)).not.toBeInTheDocument();
  const urgency = within(screen.getByRole("region", { name: "Question urgency" }));
  expect(urgency.getByText(/Judge the impact/)).toBeInTheDocument();
  expect(urgency.getByRole("list", { name: "urgency ordered levels" })).toHaveAttribute(
    "start",
    "0",
  );
  expect(urgency.getByText(/Can wait/)).toBeInTheDocument();
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
  const yes = within(screen.getByRole("group", { name: "permitted yes criterion" }));
  const no = within(screen.getByRole("group", { name: "permitted no criterion" }));
  expect(yes.getByText("null")).toBeInTheDocument();
  expect(no.queryByText("null")).not.toBeInTheDocument();
  expect(no.getByText("Not specified")).toBeInTheDocument();
});

it("renders typed answers without turning Noul into confidence or rounding Score to a level", () => {
  render(
    <ClassifierInvocationDetails
      invocation={decodeClassifierInvocation(JSON.stringify(success()))}
    />,
  );
  const route = within(screen.getByRole("region", { name: "Answer route" }));
  expect(route.getByText("0.44")).toBeInTheDocument();
  expect(route.getByRole("meter", { name: "route: review probability" })).toHaveAttribute(
    "value",
    "0.72",
  );
  expect(route.getByRole("meter", { name: "route: accept probability" })).toHaveAttribute(
    "value",
    "0.28",
  );
  const safe = within(screen.getByRole("region", { name: "Answer safe" }));
  expect(safe.getByText("0.91")).toBeInTheDocument();
  expect(safe.queryByText("Confidence")).not.toBeInTheDocument();
  expect(safe.getByRole("meter", { name: "safe: probability of yes" })).toHaveAttribute(
    "value",
    "0.91",
  );
  const urgency = within(screen.getByRole("region", { name: "Answer urgency" }));
  const scoreTerm = urgency.getByText("Score", { selector: "dt" });
  expect(scoreTerm.nextElementSibling).toHaveTextContent("0.375");
  expect(urgency.getByRole("meter", { name: "urgency: weighted score" })).toHaveAttribute(
    "value",
    "0.375",
  );
  expect(urgency.getByText(/Can wait/)).toBeInTheDocument();
  expect(urgency.getByText(/Needs action now/)).toBeInTheDocument();
  expect(urgency.getByRole("meter", { name: "urgency: 0 probability" })).toHaveAttribute(
    "value",
    "0.625",
  );
  expect(urgency.getByRole("meter", { name: "urgency: 1 probability" })).toHaveAttribute(
    "value",
    "0.375",
  );
  expect(screen.getByText("jev-1.13")).toBeInTheDocument();
  expect(screen.getByText("321")).toBeInTheDocument();
  expect(screen.getByText("47")).toBeInTheDocument();
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
  fireEvent.click(firstInput.getByRole("button", { name: "Expand context" }));
  fireEvent.click(firstInput.getByRole("button", { name: "Expand policy" }));
  expect(firstInput.getByText(/Policy one/)).toBeInTheDocument();
  expect(secondInput.queryByText(/Policy one/)).not.toBeInTheDocument();
  expect(firstCall.getByRole("meter", { name: "route: accept probability" })).toHaveAttribute(
    "value",
    "0.28",
  );
  expect(secondCall.getByRole("meter", { name: "route: accept probability" })).toHaveAttribute(
    "value",
    "0.88",
  );
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
  expect(screen.getByText("cancelled")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByRole("meter")).not.toBeInTheDocument();
});

it("keeps valid empty inputs distinct from state that failed validation before capture", () => {
  const invocation = success();
  const view = render(
    <ClassifierInvocationDetails invocation={{ ...invocation, input: "" }} />,
  );
  expect(
    within(screen.getByRole("region", { name: "Input state" })).getByText('""'),
  ).toBeInTheDocument();
  view.rerender(<ClassifierInvocationDetails invocation={{ ...invocation, input: [] }} />);
  expect(
    within(screen.getByRole("region", { name: "Input state" })).getByText("[]"),
  ).toBeInTheDocument();
  view.rerender(<ClassifierInvocationDetails invocation={{ ...invocation, input: {} }} />);
  expect(
    within(screen.getByRole("region", { name: "Input state" })).getByText("{}"),
  ).toBeInTheDocument();
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
  expect(input.queryByRole("tree")).not.toBeInTheDocument();
  expect(input.getByText(/not captured/i)).toBeInTheDocument();
});

describe("strict classifier records", () => {
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
    expect(screen.getByRole("meter", { name: "only: retain probability" })).toHaveAttribute(
      "value",
      "1",
    );
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
