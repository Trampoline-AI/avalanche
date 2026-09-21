import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  AgentEventDescriptorPage,
  ClassifierEventDescriptorPage,
  OperatorApi,
} from "./api";
import type { ClassifierDeclaration, ClassifierInvocation } from "./classifier";
import { Inspector } from "./Inspector";
import { DETAIL_CACHE_MAX_BYTES } from "./detailProjection";
import {
  AgentEventDescriptorMsg,
  ClassifierEventDescriptorMsg,
  DescriptorPageOrder,
  FlowInfoMsg,
  RunSnapshotMsg,
} from "./model";
import { createApi, node, snapshotFor, workflow } from "./test/fixtures";

const schemas = {
  inputs: [{ name: "question", type: "str", description: "Historical question" }],
  outputs: [{ name: "answer", type: "str" }],
};
const agentWorkflow = FlowInfoMsg.create({
  ...workflow,
  agentNodeIds: [node.nodeId],
  agentMetadataJson: {
    [node.nodeId]: JSON.stringify({
      signature: {
        instructions: "Follow the current instructions.",
        inputs: [{ name: "current_question", type: "str" }],
        outputs: [{ name: "current_answer", type: "str" }],
      },
      skills: [],
      tools: [],
    }),
  },
});
const run = RunSnapshotMsg.create({
  ...snapshotFor(),
  topology: {
    ...snapshotFor().topology,
    agentFieldSchemasJson: { [node.nodeId]: JSON.stringify(schemas) },
  },
  nodes: [
    {
      ...node,
      trace: {
        available: true,
        header: {
          status: "completed",
          model: "retained-model",
          subModel: "retained-sub-model",
          iterations: "2",
          maxIterations: "4",
          durationMs: "125",
          usageJson:
            '{"main":{"input_tokens":12,"output_tokens":3,"cost":0.01,"cache_hits":0},"sub":{"input_tokens":0,"output_tokens":0,"cost":0,"cache_hits":0}}',
        },
      },
    },
  ],
});
function event(sequence: number, eventKind = "iteration.recorded") {
  return AgentEventDescriptorMsg.create({
    eventSequence: String(sequence),
    eventKind,
    bodyToken: `event-${sequence}`,
    sizeBytes: "64",
    iteration: sequence,
    invocationId: "invocation-1",
  });
}
function eventPage(
  records: AgentEventDescriptorMsg[],
  nextPageToken = "",
): AgentEventDescriptorPage {
  return {
    operatorInstanceId: run.operatorInstanceId,
    asOfEventUlid: run.asOfEventUlid,
    runId: run.summary!.runId,
    nodeId: node.nodeId,
    records,
    nextPageToken,
    nextCursor: records.at(-1)?.eventSequence ?? "0",
  };
}

function traceEvent(iteration: number, sizeBytes = "64") {
  return AgentEventDescriptorMsg.create({
    ...event(iteration),
    toolCount: 1,
    predictCount: 1,
    sizeBytes,
  });
}

function traceDetail(iteration: number) {
  const usage = { input_tokens: 1, output_tokens: 1, cost: 0, cache_hits: 0 };
  return {
    event_kind: "iteration.recorded",
    data: {
      step: {
        iteration,
        reasoning: `Reasoning for turn ${iteration}.`,
        code: `print("turn ${iteration}")`,
        output: `Output for turn ${iteration}.`,
        untruncated_output: `Full output for turn ${iteration}.`,
        error: false,
        duration_ms: 1,
        usage: { main: usage, sub: usage },
        tool_calls: [
          {
            name: `lookup_${iteration}`,
            args: [],
            kwargs: {},
            result: `Tool result ${iteration}`,
            duration_ms: 1,
          },
        ],
        predict_calls: [
          {
            signature: "question -> answer",
            model: "sub-model",
            total_usage: usage,
            calls: [
              {
                duration_ms: 1,
                usage,
                input: { question: "Ready?" },
                output: { answer: `Prediction ${iteration}` },
              },
            ],
          },
        ],
      },
    },
  };
}

function traceTurn(iteration: number) {
  return screen.getByText(`Turn ${iteration}`).closest("article")!;
}

function toggleTraceSection(turn: HTMLElement, label: string) {
  fireEvent.click(within(turn).getByText(label, { selector: "summary" }));
}

const secondarySections = [
  "Generated Python",
  "Sandbox output",
  "Tools (1)",
  "Predict calls (1)",
];

function openRunIo() {
  fireEvent.click(screen.getByRole("tab", { name: "Run I/O" }));
}

describe("retained run inspection", () => {
  it("hydrates input and output independently and cancels both directions when the run changes", async () => {
    const input = Promise.withResolvers<unknown>();
    const output = Promise.withResolvers<unknown>();
    let inputSignal: AbortSignal | undefined;
    let outputSignal: AbortSignal | undefined;
    const api = createApi({
      listAgentEventPage: async (request) =>
        request.expectedRunId === "other-run"
          ? eventPage([event(3, "run.started"), event(4, "run.succeeded")])
          : eventPage([event(1, "run.started"), event(2, "run.succeeded")]),
      readJsonDetail: (token, signal) => {
        if (token === "event-1") {
          inputSignal = signal;
          return input.promise;
        }
        if (token === "event-2") {
          outputSignal = signal;
          return output.promise;
        }
        return Promise.resolve({
          inputs: { question: "new input" },
          outputs: { answer: "new output" },
        });
      },
    });
    const view = render(
      <Inspector api={api} run={run} nodeId={node.nodeId} onClose={() => undefined} />,
    );
    openRunIo();
    await waitFor(() => expect(inputSignal).toBeDefined());
    await waitFor(() => expect(outputSignal).toBeDefined());
    expect(inputSignal?.aborted).toBe(false);
    await act(async () => {
      output.resolve({ outputs: { answer: "independent output" } });
      await output.promise;
    });
    expect(await screen.findByText("independent output")).toBeInTheDocument();
    expect(inputSignal?.aborted).toBe(false);

    view.rerender(
      <Inspector
        api={api}
        run={RunSnapshotMsg.create({ ...run, summary: { ...run.summary, runId: "other-run" } })}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(inputSignal?.aborted).toBe(true);
    expect(outputSignal?.aborted).toBe(true);
    openRunIo();
    expect(await screen.findByText("new input")).toBeInTheDocument();
    expect(await screen.findByText("new output")).toBeInTheDocument();
    await act(async () => {
      input.resolve({ inputs: { question: "stale input" } });
      await input.promise;
    });
    expect(screen.queryByText("stale input")).not.toBeInTheDocument();
    expect(screen.queryByText("independent output")).not.toBeInTheDocument();
  });

  it("keeps both roots while paging beyond the descriptor window using independent cursors", async () => {
    const listAgentEventPage = vi.fn<OperatorApi["listAgentEventPage"]>(async (request) => {
      const pageIndex = request.pageToken === "events" ? 0 : Number(request.pageToken);
      const records = Array.from({ length: 100 }, (_, index) => {
        const offset = pageIndex * 100 + index;
        const sequence =
          request.order === DescriptorPageOrder.FORWARD ? offset + 1 : 600 - offset;
        // Non-turn lifecycle events avoid hydrating trace bodies before switching views.
        return event(
          sequence,
          sequence === 1
            ? "run.started"
            : sequence === 600
              ? "run.succeeded"
              : "activity.recorded",
        );
      });
      return eventPage(records, pageIndex < 5 ? String(pageIndex + 1) : "");
    });
    const api = createApi({
      listAgentEventPage,
      readJsonDetail: async () => ({
        inputs: { question: "Original input" },
        outputs: { answer: "Final output" },
      }),
    });
    render(<Inspector api={api} run={run} nodeId={node.nodeId} onClose={() => undefined} />);
    openRunIo();
    await screen.findByText("Original input");
    await screen.findByText("Final output");
    const inputField = screen.getByLabelText("question value").closest(".field-detail");
    const outputField = screen.getByLabelText("answer value").closest(".field-detail");
    expect(inputField).toHaveTextContent("Historical question");
    expect(inputField).toHaveTextContent("Original input");
    expect(outputField).toHaveTextContent("Final output");
    for (let page = 1; page < 6; page += 1) {
      const inputMore = screen.getByRole("button", { name: "Load more input events" });
      await waitFor(() => expect(inputMore).toBeEnabled());
      fireEvent.click(inputMore);
      await waitFor(() =>
        expect(listAgentEventPage).toHaveBeenCalledWith(
          expect.objectContaining({
            order: DescriptorPageOrder.FORWARD,
            pageToken: String(page),
            afterEventSequence: String(page * 100),
            beforeEventSequence: "0",
          }),
          expect.any(AbortSignal),
        ),
      );
      await waitFor(() =>
        expect(
          screen.queryByRole("button", { name: "Loading input events…" }),
        ).not.toBeInTheDocument(),
      );
    }
    expect(screen.getByText("Original input")).toBeInTheDocument();
    expect(screen.getByText("Final output")).toBeInTheDocument();
    for (let page = 1; page < 6; page += 1) {
      const outputMore = screen.getByRole("button", { name: "Load more output events" });
      await waitFor(() => expect(outputMore).toBeEnabled());
      fireEvent.click(outputMore);
      await waitFor(() =>
        expect(listAgentEventPage).toHaveBeenCalledWith(
          expect.objectContaining({
            order: DescriptorPageOrder.NEWEST_FIRST,
            pageToken: String(page),
            afterEventSequence: "0",
            beforeEventSequence: String(601 - page * 100),
          }),
          expect.any(AbortSignal),
        ),
      );
      await waitFor(() =>
        expect(
          screen.queryByRole("button", { name: "Loading output events…" }),
        ).not.toBeInTheDocument(),
      );
    }
    expect(screen.getByText("Original input")).toBeInTheDocument();
    expect(screen.getByText("Final output")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Load more .* events/ }),
    ).not.toBeInTheDocument();
  });

  it("isolates an input page failure from successful output hydration", async () => {
    const api = createApi({
      listAgentEventPage: async (request) => {
        if (request.order === DescriptorPageOrder.FORWARD)
          throw new Error("Input page unavailable");
        return eventPage([event(2, "run.succeeded")]);
      },
      readJsonDetail: async () => ({ outputs: { answer: "available output" } }),
    });
    render(<Inspector api={api} run={run} nodeId={node.nodeId} onClose={() => undefined} />);
    openRunIo();
    expect(
      await within(await screen.findByRole("region", { name: "Inputs" })).findByRole("alert"),
    ).toHaveTextContent("Input page unavailable");
    expect(await screen.findByText("available output")).toBeInTheDocument();
  });

  it("keeps run history visible when the current node is absent", async () => {
    const api = createApi({
      listAgentEventPage: async () => eventPage([event(2, "run.succeeded")]),
      readJsonDetail: async () => ({ outputs: { answer: "historical answer" } }),
    });
    const view = render(
      <Inspector
        api={api}
        workflow={agentWorkflow}
        run={run}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    openRunIo();
    expect(await screen.findByText("Historical question")).toBeInTheDocument();
    expect(screen.queryByText("current_question")).not.toBeInTheDocument();
    await screen.findByText("historical answer");
    view.rerender(
      <Inspector
        api={api}
        workflow={FlowInfoMsg.create({
          ...agentWorkflow,
          nodeIds: ["replacement"],
          displayNames: { replacement: "Fetch" },
        })}
        run={run}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(screen.getByText("historical answer")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Definition" })).not.toBeInTheDocument();
    const header = screen
      .getByRole("complementary", { name: "Run inspector" })
      .querySelector("header")!;
    expect(header).toHaveTextContent("Agent · Run run-1");
    expect(screen.queryByRole("button", { name: "See current state" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Trace" }));
    expect(screen.getByRole("region", { name: "Agent trace" })).toBeInTheDocument();
  });

  it("separates the tabless current definition from historical run schemas", async () => {
    const api = createApi();
    const view = render(
      <Inspector
        api={api}
        workflow={agentWorkflow}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    const definition = screen.getByRole("complementary", { name: "Node declaration" });
    expect(within(definition).queryByRole("tab")).not.toBeInTheDocument();
    expect(definition.querySelector("header")).toHaveTextContent("Agent · Current state");
    expect(definition).toHaveTextContent("Follow the current instructions.");
    const schemaGroup = screen.getByRole("region", { name: "Inputs and outputs" });
    expect(schemaGroup).toHaveTextContent("current_question");
    expect(schemaGroup).toHaveTextContent("current_answer");
    expect(screen.queryByText("running")).not.toBeInTheDocument();

    view.rerender(
      <Inspector
        api={api}
        workflow={agentWorkflow}
        run={RunSnapshotMsg.create({ ...run, nodes: [] })}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(screen.queryByText("Follow the current instructions.")).not.toBeInTheDocument();
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Trace",
      "Run I/O",
    ]);
    openRunIo();
    expect(await screen.findByText("Historical question")).toBeInTheDocument();
    expect(screen.queryByText("current_question")).not.toBeInTheDocument();

    view.rerender(
      <Inspector
        api={api}
        workflow={agentWorkflow}
        run={RunSnapshotMsg.create({
          ...run,
          topology: {
            ...run.topology,
            agentFieldSchemasJson: { [node.nodeId]: "{" },
          },
        })}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(
      screen.getByText("Historical input schema unavailable for this run."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Historical output schema unavailable for this run."),
    ).toBeInTheDocument();
  });

  it("shows only Trace and Run I/O in run mode and restores the selected run tab", async () => {
    const pages = vi.fn(async () => eventPage([]));
    const api = createApi({ listAgentEventPage: pages });
    const view = render(
      <Inspector
        api={api}
        workflow={agentWorkflow}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    const definition = screen.getByRole("complementary", { name: "Node declaration" });
    expect(within(definition).queryByRole("tab")).not.toBeInTheDocument();
    expect(definition).toHaveTextContent("Follow the current instructions.");
    expect(definition).toHaveTextContent("current_question");
    expect(pages).not.toHaveBeenCalled();

    view.rerender(
      <Inspector
        api={api}
        workflow={agentWorkflow}
        run={run}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Trace",
      "Run I/O",
    ]);
    const traceTab = screen.getByRole("tab", { name: "Trace" });
    expect(traceTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("region", { name: "Agent trace" })).toBeInTheDocument();
    const runInspector = screen.getByRole("complementary", { name: "Run inspector" });
    expect(runInspector.querySelector("header")).toHaveTextContent("Agent · Run run-1");
    expect(screen.queryByRole("button", { name: "See current state" })).not.toBeInTheDocument();
    await waitFor(() => expect(pages).toHaveBeenCalled());
    fireEvent.keyDown(traceTab, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Run I/O" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Historical question");
    expect(screen.queryByText("current_question")).not.toBeInTheDocument();

    view.rerender(
      <Inspector
        api={api}
        workflow={agentWorkflow}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(screen.getByText("Follow the current instructions.")).toBeInTheDocument();
    expect(screen.getByText("current_question")).toBeInTheDocument();

    view.rerender(
      <Inspector
        api={api}
        workflow={agentWorkflow}
        run={run}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(screen.getByRole("tab", { name: "Run I/O" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("keeps the selected agent tab while inspecting another node and selecting a run", () => {
    const reviewNodeId = "review";
    const reviewSchemas = {
      inputs: [{ name: "historical_review_question", type: "str" }],
      outputs: [{ name: "historical_review_answer", type: "str" }],
    };
    const multiAgentWorkflow = FlowInfoMsg.create({
      ...agentWorkflow,
      nodeIds: [node.nodeId, reviewNodeId],
      graph: {
        [node.nodeId]: { children: [reviewNodeId] },
        [reviewNodeId]: { children: [] },
      },
      nodeTypes: { ...agentWorkflow.nodeTypes, [reviewNodeId]: "agent" },
      displayNames: { ...agentWorkflow.displayNames, [reviewNodeId]: "Review" },
      agentNodeIds: [node.nodeId, reviewNodeId],
      agentMetadataJson: {
        ...agentWorkflow.agentMetadataJson,
        [reviewNodeId]: JSON.stringify({
          signature: {
            instructions: "Review the current result.",
            inputs: [{ name: "review_question", type: "str" }],
            outputs: [{ name: "review_answer", type: "str" }],
          },
          skills: [],
          tools: [],
        }),
      },
    });
    const multiAgentRun = RunSnapshotMsg.create({
      ...run,
      summary: { ...run.summary, runId: "review-run" },
      topology: {
        ...run.topology,
        nodeIds: [node.nodeId, reviewNodeId],
        graph: multiAgentWorkflow.graph,
        nodeTypes: multiAgentWorkflow.nodeTypes,
        displayNames: multiAgentWorkflow.displayNames,
        agentFieldSchemasJson: {
          ...run.topology?.agentFieldSchemasJson,
          [reviewNodeId]: JSON.stringify(reviewSchemas),
        },
      },
      nodes: [...run.nodes, { ...run.nodes[0], nodeId: reviewNodeId, name: "Review" }],
    });
    const view = render(
      <Inspector
        api={createApi()}
        workflow={multiAgentWorkflow}
        run={multiAgentRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );

    openRunIo();
    expect(screen.getByText("question")).toBeInTheDocument();
    view.rerender(
      <Inspector
        api={createApi()}
        workflow={multiAgentWorkflow}
        run={multiAgentRun}
        nodeId={reviewNodeId}
        onClose={() => undefined}
      />,
    );
    expect(screen.getByRole("tab", { name: "Run I/O" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByText("historical_review_question")).toBeInTheDocument();

    view.rerender(
      <Inspector
        api={createApi()}
        workflow={multiAgentWorkflow}
        nodeId={reviewNodeId}
        onClose={() => undefined}
      />,
    );
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Node declaration" })).toHaveTextContent(
      "review_question",
    );

    view.rerender(
      <Inspector
        api={createApi()}
        workflow={multiAgentWorkflow}
        run={multiAgentRun}
        nodeId={reviewNodeId}
        onClose={() => undefined}
      />,
    );
    expect(screen.getByRole("tab", { name: "Run I/O" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByText("historical_review_question")).toBeInTheDocument();
  });

  it("restores separate main and sub model costs and trace duration in the header on every tab", () => {
    const pricedRun = RunSnapshotMsg.create({
      ...run,
      nodes: [
        {
          ...run.nodes[0],
          trace: {
            ...run.nodes[0].trace,
            header: {
              ...run.nodes[0].trace!.header!,
              usageJson:
                '{"main":{"input_tokens":12,"output_tokens":3,"cost":0.01,"cache_hits":0},"sub":{"input_tokens":9,"output_tokens":1,"cost":0.0025,"cache_hits":0}}',
            },
          },
        },
      ],
    });
    render(
      <Inspector
        api={createApi()}
        workflow={agentWorkflow}
        run={pricedRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    for (const tab of ["Trace", "Run I/O"]) {
      fireEvent.click(screen.getByRole("tab", { name: tab }));
      const header = screen
        .getByRole("complementary", { name: "Run inspector" })
        .querySelector("header")!;
      expect(within(header).getByText("Main").parentElement).toHaveTextContent(
        "retained-model · 12 in / 3 out · $0.0100",
      );
      expect(within(header).getByText("Sub").parentElement).toHaveTextContent(
        "retained-sub-model · 9 in / 1 out · $0.0025",
      );
      expect(header).toHaveTextContent("2 of 4 iterations");
      expect(header).toHaveTextContent("0.13s");
      if (tab === "Trace") {
        const tracePanel = screen.getByRole("region", { name: "Agent trace" });
        expect(
          within(tracePanel).queryByRole("heading", { name: "Agent trace" }),
        ).not.toBeInTheDocument();
        expect(
          within(tracePanel).queryByRole("button", { name: "Details" }),
        ).not.toBeInTheDocument();
        expect(tracePanel).not.toHaveTextContent("retained-model");
        expect(tracePanel).not.toHaveTextContent("$0.0100");
      }
    }
  });

  it("opens skill Markdown separately while tool source and runtime stay inline", async () => {
    const declaration = FlowInfoMsg.create({
      ...agentWorkflow,
      agentMetadataJson: {
        [node.nodeId]: JSON.stringify({
          signature: { instructions: "Review the record." },
          models: {
            main: { identity: { name: "configured-main" } },
            sub: { identity: "configured-sub" },
          },
          skills: [
            {
              name: "Review",
              instructions: `Review every source record.\n\n\`\`\`text\n${"Retained source line.\n".repeat(300)}END_CODE\n\`\`\`\n\n## Final rule\n\nVerify the last record.`,
              packages: ["openpyxl>=3.1", "pandas==2.2.3"],
              modules: ["workbook_helpers", "evidence_utils"],
            },
            { name: "Instructions only", instructions: "Check the record." },
          ],
          tools: [
            {
              name: "Lookup",
              source_code:
                'def lookup(record_id: str):\n    """Retrieve the source evidence."""\n    return records[record_id]',
            },
          ],
          runtime: { max_iterations: 7 },
        }),
      },
    });
    render(
      <Inspector
        api={createApi()}
        workflow={declaration}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    const header = screen
      .getByRole("complementary", { name: "Node declaration" })
      .querySelector("header")!;
    expect(header).not.toHaveTextContent("configured-main");
    expect(header).not.toHaveTextContent("configured-sub");
    const models = screen.getByRole("region", { name: "Models" });
    expect(models).toHaveTextContent("configured-main");
    expect(models).toHaveTextContent("configured-sub");
    expect(models).not.toHaveTextContent("$");
    expect(screen.queryByText("Review every source record.")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Source code")).not.toBeInTheDocument();
    expect(screen.getByText("max_iterations")).not.toBeVisible();
    const skillTrigger = screen.getByRole("button", { name: "Review" });
    fireEvent.click(skillTrigger);
    const skillDialog = screen.getByRole("dialog", { name: "Review" });
    expect(within(skillDialog).getByText("Review every source record.")).toBeVisible();
    expect(within(skillDialog).getByRole("heading", { name: "Final rule" })).toBeVisible();
    expect(within(skillDialog).getByText("Verify the last record.")).toBeVisible();
    expect(
      within(skillDialog).queryByRole("button", { name: "Show more" }),
    ).not.toBeInTheDocument();
    expect(skillDialog.querySelectorAll("pre")).toHaveLength(1);
    expect(skillDialog.querySelector("pre")).toHaveTextContent("END_CODE");
    const packages = within(skillDialog).getByRole("region", { name: "Packages" });
    expect(packages).toHaveTextContent("openpyxl>=3.1");
    expect(packages).toHaveTextContent("pandas==2.2.3");
    const modules = within(skillDialog).getByRole("region", { name: "Modules" });
    expect(modules).toHaveTextContent("workbook_helpers");
    expect(modules).toHaveTextContent("evidence_utils");
    expect(
      within(screen.getByRole("region", { name: "Skills" })).queryByText(
        "Review every source record.",
      ),
    ).not.toBeInTheDocument();
    fireEvent.click(within(skillDialog).getByRole("button", { name: "Close skill" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(skillTrigger).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Instructions only" }));
    const instructionsOnlyDialog = screen.getByRole("dialog", { name: "Instructions only" });
    expect(
      within(instructionsOnlyDialog).queryByRole("region", { name: "Packages" }),
    ).not.toBeInTheDocument();
    expect(
      within(instructionsOnlyDialog).queryByRole("region", { name: "Modules" }),
    ).not.toBeInTheDocument();
    fireEvent.click(
      within(instructionsOnlyDialog).getByRole("button", { name: "Close skill" }),
    );
    fireEvent.click(skillTrigger);
    expect(screen.getByRole("dialog", { name: "Review" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Close skill" }));
    expect(screen.queryByLabelText("Source code")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Lookup", { selector: "summary" }));
    const source = await screen.findByLabelText("Source code");
    expect(source).toBeVisible();
    expect(source).toHaveTextContent("return records[record_id]");
    expect(source).toHaveTextContent("Retrieve the source evidence.");
    fireEvent.click(screen.getByText("Runtime", { selector: "summary" }));
    expect(screen.getByText("max_iterations")).toBeVisible();
  });
});

describe("trace detail retention and recovery", () => {
  it("keeps every open section across later summary hydration and releases a turn after its last section closes", async () => {
    const laterDetails = Array.from({ length: 8 }, () => Promise.withResolvers<unknown>());
    const readJsonDetail = vi.fn<OperatorApi["readJsonDetail"]>(async (token) => {
      const iteration = Number(token.slice("event-".length));
      return iteration >= 3 && iteration <= 10
        ? laterDetails[iteration - 3].promise
        : traceDetail(iteration);
    });
    const api = createApi({
      listAgentEventPage: async (request) =>
        request.pageToken === "events"
          ? eventPage(
              Array.from({ length: 10 }, (_, index) => traceEvent(index + 1)),
              "later",
            )
          : eventPage(Array.from({ length: 8 }, (_, index) => traceEvent(index + 11))),
      readJsonDetail,
    });
    render(<Inspector api={api} run={run} nodeId={node.nodeId} onClose={() => undefined} />);
    await screen.findByText("Reasoning for turn 1.");
    await screen.findByText("Reasoning for turn 2.");
    const first = traceTurn(1);
    const second = traceTurn(2);
    for (const label of secondarySections) toggleTraceSection(first, label);
    toggleTraceSection(second, "Sandbox output");
    await within(first).findByLabelText("Source code");
    await within(first).findByText(/Tool · lookup_1/);
    await within(first).findByText(/Predict · question/);
    await within(second).findByText("Output for turn 2.");
    fireEvent.click(within(first).getByRole("button", { name: "Show full output" }));

    // Closing one section must not release the body's other open consumers.
    toggleTraceSection(first, "Tools (1)");
    await waitFor(() => expect(within(first).queryByText(/Tool · lookup_1/)).toBeNull());
    for (const [index, detail] of laterDetails.entries()) {
      await act(async () => {
        detail.resolve(traceDetail(index + 3));
        await detail.promise;
      });
    }
    await screen.findByText("Reasoning for turn 10.");
    expect(within(first).getByText("Full output for turn 1.")).toBeVisible();
    expect(within(first).getByLabelText("Source code")).toHaveTextContent('print("turn 1")');
    expect(within(first).getByText(/Predict · question/)).toBeVisible();
    expect(within(second).getByText("Output for turn 2.")).toBeVisible();
    toggleTraceSection(first, "Tools (1)");
    expect(await within(first).findByText(/Tool · lookup_1/)).toBeVisible();
    expect(readJsonDetail.mock.calls.filter(([token]) => token === "event-1")).toHaveLength(1);
    expect(readJsonDetail.mock.calls.filter(([token]) => token === "event-2")).toHaveLength(1);

    for (const label of secondarySections) toggleTraceSection(first, label);
    await waitFor(() =>
      expect(within(first).queryByText("Full output for turn 1.")).toBeNull(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Load more trace" }));
    await screen.findByText("Reasoning for turn 18.");
    expect(within(second).getByText("Output for turn 2.")).toBeVisible();
    toggleTraceSection(first, "Generated Python");
    expect(await within(first).findByLabelText("Source code")).toHaveTextContent(
      'print("turn 1")',
    );
    expect(readJsonDetail.mock.calls.filter(([token]) => token === "event-1")).toHaveLength(2);
    expect(readJsonDetail.mock.calls.filter(([token]) => token === "event-2")).toHaveLength(1);
  });

  it.each([
    { limit: "entry", turnCount: 9, sizeBytes: "64" },
    { limit: "byte", turnCount: 2, sizeBytes: String(DETAIL_CACHE_MAX_BYTES / 2 + 1) },
  ])(
    "offers explicit recovery when open turns exceed the $limit budget, without refetch loops",
    async ({ turnCount, sizeBytes }) => {
      const readJsonDetail = vi.fn<OperatorApi["readJsonDetail"]>(async (token) =>
        traceDetail(Number(token.slice("event-".length))),
      );
      const api = createApi({ readJsonDetail });
      const view = render(
        <Inspector api={api} run={run} nodeId={node.nodeId} onClose={() => undefined} />,
      );
      const liveEvents: AgentEventDescriptorMsg[] = [];
      for (let iteration = 1; iteration <= turnCount; iteration += 1) {
        liveEvents.push(traceEvent(iteration, sizeBytes));
        view.rerender(
          <Inspector
            api={api}
            run={run}
            nodeId={node.nodeId}
            liveEvents={[...liveEvents]}
            onClose={() => undefined}
          />,
        );
        await screen.findByText(`Reasoning for turn ${iteration}.`);
        toggleTraceSection(traceTurn(iteration), "Sandbox output");
        expect(
          await within(traceTurn(iteration)).findByText(`Output for turn ${iteration}.`),
        ).toBeVisible();
      }
      const first = traceTurn(1);
      expect(within(first).queryByText("Output for turn 1.")).toBeNull();
      expect(within(first).queryByRole("status")).toBeNull();
      expect(readJsonDetail).toHaveBeenCalledTimes(turnCount + 1);
      fireEvent.click(within(first).getByRole("button", { name: "Reload step detail" }));
      expect(await within(first).findByText("Output for turn 1.")).toBeVisible();
      expect(
        within(traceTurn(2)).getByRole("button", { name: "Reload step detail" }),
      ).toBeVisible();
      expect(readJsonDetail).toHaveBeenCalledTimes(turnCount + 2);
    },
  );

  it("retries failed reasoning in the same inspector scope and restores all secondary sections", async () => {
    const retry = Promise.withResolvers<unknown>();
    const readJsonDetail = vi
      .fn<OperatorApi["readJsonDetail"]>()
      .mockRejectedValueOnce(new Error("Temporary detail failure"))
      .mockReturnValueOnce(retry.promise);
    const api = createApi({
      listAgentEventPage: async () => eventPage([traceEvent(1)]),
      readJsonDetail,
    });
    render(<Inspector api={api} run={run} nodeId={node.nodeId} onClose={() => undefined} />);
    const retryButton = await screen.findByRole("button", { name: "Retry turn 1" });
    expect(screen.getByRole("alert")).toHaveTextContent("Temporary detail failure");
    expect(screen.queryByText("Generated Python")).toBeNull();
    expect(readJsonDetail).toHaveBeenCalledTimes(1);
    fireEvent.click(retryButton);
    expect(screen.getByRole("status")).toHaveTextContent("Loading reasoning");
    expect(screen.queryByRole("button", { name: "Retry turn 1" })).toBeNull();
    await act(async () => {
      retry.resolve(traceDetail(1));
      await retry.promise;
    });
    expect(await screen.findByText("Reasoning for turn 1.")).toBeVisible();
    const turn = traceTurn(1);
    for (const label of secondarySections) toggleTraceSection(turn, label);
    expect(await within(turn).findByLabelText("Source code")).toHaveTextContent(
      'print("turn 1")',
    );
    expect(await within(turn).findByText("Output for turn 1.")).toBeVisible();
    expect(await within(turn).findByText(/Tool · lookup_1/)).toBeVisible();
    expect(await within(turn).findByText(/Predict · question/)).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(readJsonDetail).toHaveBeenCalledTimes(2);
  });

  it("cancels a retry on tab change without letting its late completion interfere with the current request", async () => {
    const stale = Promise.withResolvers<unknown>();
    const current = Promise.withResolvers<unknown>();
    const readJsonDetail = vi
      .fn<OperatorApi["readJsonDetail"]>()
      .mockRejectedValueOnce(new Error("Temporary failure"))
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(current.promise)
      .mockResolvedValue(traceDetail(1));
    const api = createApi({
      listAgentEventPage: async () => eventPage([traceEvent(1)]),
      readJsonDetail,
    });
    const view = render(
      <Inspector api={api} run={run} nodeId={node.nodeId} onClose={() => undefined} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Retry turn 1" }));
    const staleSignal = readJsonDetail.mock.calls[1][1];
    openRunIo();
    expect(staleSignal?.aborted).toBe(true);
    fireEvent.click(screen.getByRole("tab", { name: "Trace" }));
    await waitFor(() => expect(readJsonDetail).toHaveBeenCalledTimes(3));
    await act(async () => {
      const body = traceDetail(1);
      body.data.step.reasoning = "Stale reasoning";
      stale.resolve(body);
      await stale.promise;
    });
    view.rerender(
      <Inspector
        api={api}
        run={run}
        nodeId={node.nodeId}
        liveEvents={[event(2, "code.generated")]}
        onClose={() => undefined}
      />,
    );
    expect(screen.queryByText("Stale reasoning")).toBeNull();
    expect(within(traceTurn(1)).getByRole("status")).toHaveTextContent("Loading reasoning");
    expect(readJsonDetail).toHaveBeenCalledTimes(3);
    await act(async () => {
      current.resolve(traceDetail(1));
      await current.promise;
    });
    await screen.findByText("Reasoning for turn 1.");
    toggleTraceSection(traceTurn(1), "Sandbox output");
    await screen.findByText("Output for turn 1.");
    view.rerender(
      <Inspector
        api={api}
        run={RunSnapshotMsg.create({ ...run, summary: { ...run.summary, runId: "new-run" } })}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    await waitFor(() => expect(readJsonDetail).toHaveBeenCalledTimes(4));
    await screen.findByText("Reasoning for turn 1.");
    expect(screen.queryByText("Output for turn 1.")).toBeNull();
    toggleTraceSection(traceTurn(1), "Sandbox output");
    expect(await screen.findByText("Output for turn 1.")).toBeVisible();
  });

  it("retains reasoning but rejects oversized secondary bodies without refetching them on open", async () => {
    const readJsonDetail = vi
      .fn<OperatorApi["readJsonDetail"]>()
      .mockResolvedValue(traceDetail(1));
    const api = createApi({
      listAgentEventPage: async () =>
        eventPage([traceEvent(1, String(DETAIL_CACHE_MAX_BYTES + 1))]),
      readJsonDetail,
    });
    render(<Inspector api={api} run={run} nodeId={node.nodeId} onClose={() => undefined} />);
    expect(await screen.findByText("Reasoning for turn 1.")).toBeVisible();
    const turn = traceTurn(1);
    toggleTraceSection(turn, "Generated Python");
    expect(await within(turn).findByRole("alert")).toHaveTextContent("browser detail limit");
    expect(within(turn).queryByLabelText("Source code")).toBeNull();
    expect(within(turn).queryByRole("button", { name: /Retry|Reload/ })).toBeNull();
    toggleTraceSection(turn, "Generated Python");
    await waitFor(() => expect(within(turn).queryByRole("alert")).toBeNull());
    toggleTraceSection(turn, "Generated Python");
    expect(await within(turn).findByRole("alert")).toHaveTextContent("browser detail limit");
    expect(readJsonDetail).toHaveBeenCalledTimes(1);
  });
});

describe("regular step inspection", () => {
  it("refreshes source on definition and API changes and rejects stale source completion", async () => {
    const oldSource = Promise.withResolvers<string>();
    let oldSignal: AbortSignal | undefined;
    let calls = 0;
    const api = createApi({
      getWorkflowNodeSource: (_workflow, _node, signal) => {
        calls += 1;
        if (calls === 1) {
          oldSignal = signal;
          return oldSource.promise;
        }
        return Promise.resolve(`return 'definition ${calls}'`);
      },
    });
    const view = render(
      <Inspector
        api={api}
        workflow={workflow}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    await waitFor(() => expect(oldSignal).toBeDefined());
    const updatedWorkflow = FlowInfoMsg.create({
      ...workflow,
      displayNames: { fetch: "Updated fetch" },
    });
    view.rerender(
      <Inspector
        api={api}
        workflow={updatedWorkflow}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(oldSignal?.aborted).toBe(true);
    expect(await screen.findByLabelText("Source code")).toHaveTextContent("definition 2");
    await act(async () => {
      oldSource.resolve("return 'stale definition'");
      await oldSource.promise;
    });
    expect(screen.getByLabelText("Source code")).not.toHaveTextContent("stale definition");
    const reloadedWorkflow = FlowInfoMsg.create(updatedWorkflow);
    view.rerender(
      <Inspector
        api={api}
        workflow={reloadedWorkflow}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Source code")).toHaveTextContent("definition 3"),
    );
    view.rerender(
      <Inspector
        api={createApi({ getWorkflowNodeSource: async () => "return 'new API'" })}
        workflow={reloadedWorkflow}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Source code")).toHaveTextContent("new API"),
    );
  });

  it("omits standard run inspection without loading current source code", () => {
    const getWorkflowNodeSource = vi.fn(async () => "return 'step result'");
    const stepRun = RunSnapshotMsg.create({
      ...snapshotFor(),
      nodes: [{ ...node, runningElapsedSeconds: 2.5, error: "Step error" }],
    });
    render(
      <Inspector
        api={createApi({ getWorkflowNodeSource })}
        workflow={workflow}
        run={stepRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Source code")).not.toBeInTheDocument();
    expect(getWorkflowNodeSource).not.toHaveBeenCalled();
  });
});

const classifierDeclaration: ClassifierDeclaration = {
  input_schema: {
    title: "HistoricalInput",
    type: "object",
    properties: { historical_state: { type: "string" } },
    required: ["historical_state"],
  },
  step_inputs: [
    {
      name: "historical_arg",
      type_name: "str",
      json_schema: { type: "string" },
      required: true,
    },
  ],
  step_output: { type_name: "bool", json_schema: { type: "boolean" } },
  questions: {
    category: {
      type: "choice",
      instructions: "Choose the historical category.",
      criteria: { keep: "Useful", discard: "Not useful" },
    },
    approved: { type: "noul", instructions: "Is it approved?", criteria: null },
    quality: { type: "score", instructions: ["Assess quality"], criteria: ["Low", "High"] },
  },
  runtime: { model: "jev-latest", timeout: 10 },
};
const classifierWorkflow = FlowInfoMsg.create({
  ...workflow,
  classifierMetadataJson: { [node.nodeId]: JSON.stringify(classifierDeclaration) },
});
const classifierRun = RunSnapshotMsg.create({
  ...snapshotFor(),
  topology: {
    ...snapshotFor().topology,
    classifierMetadataJson: classifierWorkflow.classifierMetadataJson,
  },
});
function classifierInvocation(
  index: number,
  status: ClassifierInvocation["status"] = "success",
): ClassifierInvocation {
  return {
    invocation_id: `classification-${index}`,
    invocation_index: index,
    status,
    started_at: 100,
    ended_at: status === "running" ? null : 101,
    declaration: classifierDeclaration,
    input: { record: `input-${index}`, approved: index === 0 },
    error: status === "failed" ? "Classification request failed" : null,
    result:
      status === "success"
        ? {
            model: `result-model-${index}`,
            usage: { input_tokens: 10, output_tokens: 5 },
            answers: {
              category: {
                type: "choice",
                choice: "keep",
                probabilities: { keep: 0.8, discard: 0.2 },
                confidence: 0.7,
              },
              approved: { type: "noul", noul: 0.9 },
              quality: {
                type: "score",
                score: 0.75,
                legend: { "0": "Low", "1": "High" },
                probabilities: { "0": 0.25, "1": 0.75 },
                confidence: 0.6,
              },
            },
          }
        : null,
  };
}
function classifierEvent(
  sequence: number,
  index: number,
  status: ClassifierInvocation["status"] = "success",
) {
  return ClassifierEventDescriptorMsg.create({
    eventSequence: String(sequence),
    invocationId: `classification-${index}`,
    invocationIndex: index,
    answers:
      status === "success"
        ? [
            { questionId: "category", type: "choice", choice: "keep" },
            { questionId: "approved", type: "noul", noul: 0.9 },
            { questionId: "quality", type: "score", score: 0.75 },
          ]
        : [],
    eventKind: status,
    bodyToken: `classifier-${sequence}`,
    sizeBytes: "64",
    error: status === "failed",
  });
}
function classifierPage(
  records: ClassifierEventDescriptorMsg[],
  nextPageToken = "",
): ClassifierEventDescriptorPage {
  return {
    operatorInstanceId: classifierRun.operatorInstanceId,
    asOfEventUlid: classifierRun.asOfEventUlid,
    runId: classifierRun.summary!.runId,
    nodeId: node.nodeId,
    records,
    nextPageToken,
    nextCursor: records.at(-1)?.eventSequence ?? "0",
  };
}

describe("classifier inspection", () => {
  it("shows declared questions before a run without fetching execution or source", () => {
    const listClassifierEventPage = vi.fn<OperatorApi["listClassifierEventPage"]>();
    const getWorkflowNodeSource = vi.fn<OperatorApi["getWorkflowNodeSource"]>();
    render(
      <Inspector
        api={createApi({ listClassifierEventPage, getWorkflowNodeSource })}
        workflow={classifierWorkflow}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    const declaration = screen.getByRole("complementary", { name: "Classifier declaration" });
    expect(within(declaration).getByRole("tab", { name: "Definition" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(within(declaration).getByRole("tab", { name: "Code" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
    expect(within(declaration).getByLabelText("Question category")).toHaveTextContent(
      "Choose the historical category.",
    );
    expect(within(declaration).getByLabelText("Question approved")).toHaveTextContent(
      "Is it approved?",
    );
    expect(within(declaration).getByLabelText("Question quality")).toHaveTextContent(
      "Assess quality",
    );
    expect(listClassifierEventPage).not.toHaveBeenCalled();
    expect(getWorkflowNodeSource).not.toHaveBeenCalled();
  });

  it("loads source only in Code and cancels stale requests across tabs, definitions, APIs, and selections", async () => {
    const pendingSource = Promise.withResolvers<string>();
    const getWorkflowNodeSource = vi
      .fn<OperatorApi["getWorkflowNodeSource"]>()
      .mockImplementationOnce(() => pendingSource.promise)
      .mockResolvedValue("return 'current classifier'");
    const api = createApi({ getWorkflowNodeSource });
    const view = render(
      <Inspector
        api={api}
        workflow={classifierWorkflow}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Code" }));
    expect(getWorkflowNodeSource).toHaveBeenCalledWith(
      classifierWorkflow.name,
      node.nodeId,
      expect.any(AbortSignal),
    );
    const firstSignal = getWorkflowNodeSource.mock.calls[0][2];
    fireEvent.click(screen.getByRole("tab", { name: "Definition" }));
    expect(firstSignal?.aborted).toBe(true);
    await act(async () => {
      pendingSource.resolve("return 'stale classifier'");
      await pendingSource.promise;
    });
    expect(screen.queryByLabelText("Source code")).toBeNull();
    fireEvent.keyDown(screen.getByRole("tab", { name: "Definition" }), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Code" })).toHaveFocus();
    expect(await screen.findByLabelText("Source code")).toHaveTextContent("current classifier");
    const updatedWorkflow = FlowInfoMsg.create(classifierWorkflow);
    const definitionSource = Promise.withResolvers<string>();
    getWorkflowNodeSource.mockImplementationOnce(() => definitionSource.promise);
    view.rerender(
      <Inspector
        api={api}
        workflow={updatedWorkflow}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    const definitionSignal = getWorkflowNodeSource.mock.calls[2][2];
    expect(screen.queryByLabelText("Source code")).toBeNull();
    const apiSource = Promise.withResolvers<string>();
    const replacementSource = vi.fn<OperatorApi["getWorkflowNodeSource"]>(
      () => apiSource.promise,
    );
    const replacementApi = createApi({ getWorkflowNodeSource: replacementSource });
    view.rerender(
      <Inspector
        api={replacementApi}
        workflow={updatedWorkflow}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(definitionSignal?.aborted).toBe(true);
    await act(async () => {
      definitionSource.resolve("return 'obsolete definition'");
      await definitionSource.promise;
    });
    expect(screen.queryByLabelText("Source code")).toBeNull();
    const replacementSignal = replacementSource.mock.calls[0][2];
    view.rerender(
      <Inspector
        api={replacementApi}
        workflow={updatedWorkflow}
        run={classifierRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(replacementSignal?.aborted).toBe(true);
    expect(screen.queryByRole("tab", { name: "Calls" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Code" })).toBeNull();
    await act(async () => {
      apiSource.resolve("return 'current code must not enter history'");
      await apiSource.promise;
    });
    expect(screen.queryByLabelText("Source code")).toBeNull();
    expect(screen.queryByRole("tab", { name: "Definition" })).toBeNull();
    expect(replacementSource).toHaveBeenCalledTimes(1);
  });

  it("presents unavailable source and retries failures only on explicit request", async () => {
    const getWorkflowNodeSource = vi
      .fn<OperatorApi["getWorkflowNodeSource"]>()
      .mockRejectedValueOnce(new Error("Source offline"))
      .mockResolvedValueOnce(undefined);
    render(
      <Inspector
        api={createApi({ getWorkflowNodeSource })}
        workflow={classifierWorkflow}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Code" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Source offline");
    expect(getWorkflowNodeSource).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Retry source code" }));
    expect(await screen.findByText("Source code is unavailable for this node.")).toBeVisible();
    expect(getWorkflowNodeSource).toHaveBeenCalledTimes(2);
  });

  it("aborts source on node selection and never shows another classifier's code", async () => {
    const oldSource = Promise.withResolvers<string>();
    const otherNodeId = "other-classifier";
    const twoClassifiers = FlowInfoMsg.create({
      ...classifierWorkflow,
      nodeIds: [node.nodeId, otherNodeId],
      classifierMetadataJson: {
        ...classifierWorkflow.classifierMetadataJson,
        [otherNodeId]: JSON.stringify(classifierDeclaration),
      },
    });
    const getWorkflowNodeSource = vi.fn<OperatorApi["getWorkflowNodeSource"]>(
      (_workflow, selectedNode) =>
        selectedNode === node.nodeId
          ? oldSource.promise
          : Promise.resolve("return 'other classifier'"),
    );
    const api = createApi({ getWorkflowNodeSource });
    const view = render(
      <Inspector
        api={api}
        workflow={twoClassifiers}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Code" }));
    const oldSignal = getWorkflowNodeSource.mock.calls[0][2];
    view.rerender(
      <Inspector
        api={api}
        workflow={twoClassifiers}
        nodeId={otherNodeId}
        onClose={() => undefined}
      />,
    );
    expect(oldSignal?.aborted).toBe(true);
    expect(screen.getByRole("tab", { name: "Definition" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(getWorkflowNodeSource).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("tab", { name: "Code" }));
    expect(await screen.findByLabelText("Source code")).toHaveTextContent("other classifier");
    await act(async () => {
      oldSource.resolve("return 'stale other node'");
      await oldSource.promise;
    });
    expect(screen.getByLabelText("Source code")).not.toHaveTextContent("stale other node");
  });

  it("shows only calls in a run, independent of current definition changes and deletion", async () => {
    const api = createApi({
      listClassifierEventPage: async () => classifierPage([classifierEvent(1, 0)]),
      readJsonDetail: async () => classifierInvocation(0),
    });
    const changedWorkflow = FlowInfoMsg.create({
      ...classifierWorkflow,
      classifierMetadataJson: {
        [node.nodeId]: JSON.stringify({
          ...classifierDeclaration,
          input_schema: { type: "object", properties: { current_state: { type: "number" } } },
          step_inputs: [],
          step_output: { type_name: "int", json_schema: { type: "integer" } },
          questions: {
            replacement: { type: "noul", instructions: "New current question", criteria: null },
          },
          runtime: classifierDeclaration.runtime,
        }),
      },
    });
    const view = render(
      <Inspector
        api={api}
        workflow={changedWorkflow}
        run={classifierRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(screen.queryByRole("tab", { name: "Calls" })).toBeNull();
    expect(screen.queryByLabelText("Historical classifier declaration")).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "Call 1" }));
    const answer = await screen.findByLabelText("Answer category");
    expect(answer).toHaveTextContent("keep");
    expect(screen.queryByRole("tab", { name: "Definition" })).toBeNull();
    expect(screen.queryByLabelText("Question category")).toBeNull();
    expect(within(screen.getByLabelText("Answer approved")).getByRole("meter")).toHaveAttribute(
      "aria-valuenow",
      "0.9",
    );
    expect(
      within(screen.getByLabelText("Answer approved")).queryByText("Confidence"),
    ).toBeNull();
    expect(screen.getByLabelText("Answer quality")).toHaveTextContent("0.75");
    expect(screen.queryByText("New current question")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Expand invocation definition" }));
    const definition = within(screen.getByRole("region", { name: "Invocation definition" }));
    expect(definition.getByRole("region", { name: "Step inputs" })).toHaveTextContent(
      "historical_arg",
    );
    fireEvent.click(definition.getByRole("button", { name: "Expand state schema" }));
    expect(definition.getByRole("region", { name: "Classifier input" })).toHaveTextContent(
      "historical_state",
    );
    expect(definition.queryByText("current_state")).toBeNull();
    expect(definition.queryByText("New current question")).toBeNull();
    view.rerender(
      <Inspector
        api={api}
        run={classifierRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(screen.queryByRole("tab", { name: "Definition" })).toBeNull();
    expect(screen.getByLabelText("Answer category")).toBeVisible();
    expect(definition.getByRole("region", { name: "Classifier input" })).toHaveTextContent(
      "historical_state",
    );
  });

  it("groups lifecycle records by call and pairs each selected input with its own typed answers", async () => {
    const secondInvocation = classifierInvocation(1);
    secondInvocation.input = ["different input", { document: "second" }];
    if (secondInvocation.result) {
      secondInvocation.result.answers.category = {
        type: "choice",
        choice: "discard",
        probabilities: { keep: 0.1, discard: 0.9 },
        confidence: 0.85,
      };
    }
    const readJsonDetail = vi.fn<OperatorApi["readJsonDetail"]>(async (token) =>
      token === "classifier-1"
        ? classifierInvocation(0, "running")
        : token === "classifier-2"
          ? classifierInvocation(0)
          : secondInvocation,
    );
    const api = createApi({ readJsonDetail });
    const view = render(
      <Inspector
        api={api}
        run={classifierRun}
        nodeId={node.nodeId}
        liveClassifierEvents={[classifierEvent(1, 0, "running")]}
        onClose={() => undefined}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Call 1" }));
    expect(await screen.findByLabelText("Input state")).toHaveTextContent("input-0");
    expect(screen.getByRole("status", { name: "Invocation status" })).toHaveTextContent(
      "running",
    );
    expect(screen.queryByLabelText("Classification answers")).toBeNull();
    view.rerender(
      <Inspector
        api={api}
        run={classifierRun}
        nodeId={node.nodeId}
        liveClassifierEvents={[
          classifierEvent(1, 0, "running"),
          classifierEvent(2, 0),
          ClassifierEventDescriptorMsg.create({
            ...classifierEvent(3, 1),
            answers: [
              { questionId: "category", type: "choice", choice: "discard" },
              { questionId: "approved", type: "noul", noul: 0.9 },
              { questionId: "quality", type: "score", score: 0.75 },
            ],
          }),
          classifierEvent(4, 0, "running"),
        ]}
        onClose={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Call 2" }));
    const second = screen.getByLabelText("Invocation classification-1");
    await waitFor(() =>
      expect(within(second).getByLabelText("Input state")).toHaveTextContent("different input"),
    );
    expect(within(second).getByLabelText("Answer category")).toHaveTextContent("discard");
    const first = screen.getByLabelText("Invocation classification-0");
    fireEvent.click(within(first).getByRole("button", { expanded: false }));
    await waitFor(() =>
      expect(within(first).getByLabelText("Input state")).toHaveTextContent("input-0"),
    );
    expect(within(first).getByLabelText("Input state")).not.toHaveTextContent(
      "different input",
    );
    expect(within(first).getByLabelText("Answer category")).toHaveTextContent("keep");
    expect(within(second).queryByLabelText("Input state")).toBeNull();
    fireEvent.click(within(second).getByRole("button", { expanded: false }));
    await waitFor(() =>
      expect(within(second).getByLabelText("Input state")).toHaveTextContent("different input"),
    );
    expect(within(first).queryByLabelText("Input state")).toBeNull();
    expect(screen.getAllByRole("rowgroup", { name: /^Invocation / })).toHaveLength(2);
    expect(within(first).getByRole("status", { name: "Invocation status" })).toHaveTextContent(
      "success",
    );
    expect(within(second).getByRole("status", { name: "Invocation status" })).toHaveTextContent(
      "success",
    );
    expect(readJsonDetail.mock.calls.some(([token]) => token === "classifier-4")).toBe(false);
  });

  it("keeps failed and cancelled calls paired with captured input and distinguishes validation failure", async () => {
    const failed = classifierInvocation(0, "failed");
    const cancelled = classifierInvocation(1, "cancelled");
    const invalid = { ...classifierInvocation(2, "failed"), input: null };
    render(
      <Inspector
        api={createApi({
          listClassifierEventPage: async () =>
            classifierPage([
              classifierEvent(1, 0, "failed"),
              classifierEvent(2, 1, "cancelled"),
              classifierEvent(3, 2, "failed"),
            ]),
          readJsonDetail: async (token) =>
            token === "classifier-1" ? failed : token === "classifier-2" ? cancelled : invalid,
        })}
        run={classifierRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Call 3" }));
    expect(await screen.findByLabelText("Input state")).toHaveTextContent(/not captured/i);
    expect(screen.queryByLabelText("Classification answers")).toBeNull();
    const cancelledCall = screen.getByLabelText("Invocation classification-1");
    fireEvent.click(within(cancelledCall).getByRole("button", { expanded: false }));
    expect(await within(cancelledCall).findByLabelText("Input state")).toHaveTextContent(
      "input-1",
    );
    expect(
      within(cancelledCall).getByRole("status", { name: "Invocation status" }),
    ).toHaveTextContent("cancelled");
    expect(within(cancelledCall).queryByLabelText("Classification answers")).toBeNull();
    const failedCall = screen.getByLabelText("Invocation classification-0");
    fireEvent.click(within(failedCall).getByRole("button", { expanded: false }));
    expect(await within(failedCall).findByLabelText("Input state")).toHaveTextContent(
      "input-0",
    );
    expect(within(failedCall).getByRole("alert")).toHaveTextContent(
      "Classification request failed",
    );
    expect(
      within(failedCall).getByRole("status", { name: "Invocation status" }),
    ).toHaveTextContent("failed");
    expect(within(failedCall).queryByLabelText("Classification answers")).toBeNull();
  });

  it("aborts both page and body requests on selection changes and discards late completions", async () => {
    const oldPage = Promise.withResolvers<ClassifierEventDescriptorPage>();
    const oldBody = Promise.withResolvers<unknown>();
    let pageSignal: AbortSignal | undefined;
    let bodySignal: AbortSignal | undefined;
    const api = createApi({
      listClassifierEventPage: (request, signal) => {
        if (request.expectedRunId === classifierRun.summary!.runId) {
          pageSignal = signal;
          return oldPage.promise;
        }
        return Promise.resolve({
          ...classifierPage([classifierEvent(3, 1)]),
          runId: request.expectedRunId,
        });
      },
      readJsonDetail: (token, signal) => {
        if (token === "classifier-1") {
          bodySignal = signal;
          return oldBody.promise;
        }
        return Promise.resolve(classifierInvocation(1));
      },
    });
    const view = render(
      <Inspector
        api={api}
        run={classifierRun}
        nodeId={node.nodeId}
        liveClassifierEvents={[classifierEvent(1, 0)]}
        onClose={() => undefined}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Call 1" }));
    await waitFor(() => expect(bodySignal).toBeDefined());
    const otherRun = RunSnapshotMsg.create({
      ...classifierRun,
      summary: { ...classifierRun.summary, runId: "other-run" },
    });
    view.rerender(
      <Inspector api={api} run={otherRun} nodeId={node.nodeId} onClose={() => undefined} />,
    );
    expect(pageSignal?.aborted).toBe(true);
    expect(bodySignal?.aborted).toBe(true);
    expect(screen.queryByRole("button", { expanded: true })).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "Call 2" }));
    expect(await screen.findByLabelText("Input state")).toHaveTextContent("input-1");
    await act(async () => {
      oldPage.resolve(classifierPage([classifierEvent(1, 0)]));
      oldBody.resolve(classifierInvocation(0));
      await Promise.all([oldPage.promise, oldBody.promise]);
    });
    expect(screen.getByLabelText("Input state")).not.toHaveTextContent("input-0");
    expect(screen.queryByRole("rowgroup", { name: "Invocation classification-0" })).toBeNull();
  });

  it("distinguishes page failure, body failure, and a genuinely uninvoked classifier, with explicit retries", async () => {
    let pageAttempts = 0;
    let detailAttempts = 0;
    const api = createApi({
      listClassifierEventPage: async () => {
        if (++pageAttempts === 1) throw new Error("Page offline");
        return classifierPage([classifierEvent(1, 0)]);
      },
      readJsonDetail: async () => {
        if (++detailAttempts === 1) throw new Error("Body offline");
        return classifierInvocation(0);
      },
    });
    const view = render(
      <Inspector
        api={api}
        run={classifierRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(await screen.findByText(/Page offline/)).toBeVisible();
    expect(screen.queryByText("Classifier not invoked in this run.")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry classifier history" }));
    fireEvent.click(await screen.findByRole("button", { name: "Call 1" }));
    expect(await screen.findByText(/Body offline/)).toBeVisible();
    expect(screen.queryByText("Classifier not invoked in this run.")).toBeNull();
    const call = screen.getByLabelText("Invocation classification-0");
    fireEvent.click(within(call).getByRole("button", { expanded: true }));
    fireEvent.click(within(call).getByRole("button", { expanded: false }));
    expect(within(call).getByRole("alert")).toHaveTextContent("Body offline");
    expect(detailAttempts).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Retry invocation details" }));
    expect(await screen.findByLabelText("Input state")).toHaveTextContent("input-0");
    view.rerender(
      <Inspector
        api={createApi()}
        run={classifierRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(await screen.findByText("Classifier not invoked in this run.")).toBeVisible();
    expect(screen.queryByLabelText("Input state")).toBeNull();
  });

  it("shows expired invocation statuses without losing retained answers or offering unavailable body requests", async () => {
    const statuses = ["running", "success", "failed", "cancelled"] as const;
    const expired = statuses.map((status, index) =>
      ClassifierEventDescriptorMsg.create({
        ...classifierEvent(index + 1, index, status),
        bodyToken: "",
      }),
    );
    const readJsonDetail = vi.fn<OperatorApi["readJsonDetail"]>(async () =>
      classifierInvocation(4),
    );
    render(
      <Inspector
        api={createApi({
          listClassifierEventPage: async () =>
            classifierPage([...expired, classifierEvent(5, 4)]),
          readJsonDetail,
        })}
        run={classifierRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Call 5" }));
    expect(await screen.findByLabelText("Input state")).toHaveTextContent("input-4");
    for (const [index, status] of statuses.entries()) {
      const invocation = screen.getByRole("rowgroup", {
        name: `Invocation classification-${index}`,
      });
      fireEvent.click(within(invocation).getByRole("button", { expanded: false }));
      expect(
        within(invocation).getByRole("status", { name: "Invocation status" }),
      ).toHaveTextContent(status);
      expect(within(invocation).getByText(/detail unavailable/i)).toBeVisible();
      const summary = within(invocation).getByRole("button", { expanded: true });
      expect(summary).toHaveAccessibleName(`Call ${index + 1}`);
      if (status === "success") {
        expect(invocation).toHaveTextContent("category: keep · approved: 0.9 · quality: 0.75");
      }
      expect(within(invocation).queryByLabelText("Classification answers")).toBeNull();
      expect(
        within(invocation).queryByRole("button", { name: /invocation details/i }),
      ).toBeNull();
    }
    expect(screen.queryByRole("alert")).toBeNull();
    expect(readJsonDetail).toHaveBeenCalledTimes(1);
  });

  it("does not substitute current questions when retained declaration or detail is malformed", async () => {
    const malformedRun = RunSnapshotMsg.create({
      ...classifierRun,
      topology: { ...classifierRun.topology, classifierMetadataJson: { [node.nodeId]: "{}" } },
    });
    render(
      <Inspector
        api={createApi({
          listClassifierEventPage: async () => classifierPage([classifierEvent(1, 0)]),
          readJsonDetail: async () => classifierInvocation(1),
        })}
        workflow={classifierWorkflow}
        run={malformedRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(screen.queryByRole("tab", { name: "Definition" })).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "Call 1" }));
    expect(await screen.findByText(/does not match its invocation descriptor/)).toBeVisible();
    expect(screen.queryByLabelText("Question category")).toBeNull();
    expect(screen.queryByLabelText("Classification answers")).toBeNull();
  });

  it("derives interruption from terminal node state while retaining successful calls after postprocessing failure", async () => {
    const api = createApi({
      listClassifierEventPage: async () =>
        classifierPage([classifierEvent(1, 0, "running"), classifierEvent(2, 1)]),
      readJsonDetail: async (token) =>
        token === "classifier-1" ? classifierInvocation(0, "running") : classifierInvocation(1),
    });
    const stoppedRun = RunSnapshotMsg.create({
      ...classifierRun,
      nodes: [{ ...node, status: "failed", error: "Postprocessing failed" }],
    });
    render(
      <Inspector api={api} run={stoppedRun} nodeId={node.nodeId} onClose={() => undefined} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Call 2" }));
    expect(await screen.findByLabelText("Input state")).toHaveTextContent("input-1");
    expect(screen.getByLabelText("Answer category")).toHaveTextContent("keep");
    const interrupted = screen.getByRole("rowgroup", { name: "Invocation classification-0" });
    fireEvent.click(within(interrupted).getByRole("button", { expanded: false }));
    expect(await within(interrupted).findByLabelText("Input state")).toHaveTextContent(
      "input-0",
    );
    expect(
      within(interrupted).getByRole("status", { name: "Invocation status" }),
    ).toHaveTextContent("interrupted");
    expect(within(interrupted).queryByLabelText("Classification answers")).toBeNull();
    expect(screen.getByText("Postprocessing failed")).toBeVisible();
  });

  it("pages calls in ascending index order and keeps them collapsed on load, paging, and live arrivals", async () => {
    const events = Array.from({ length: 60 }, (_, index) => classifierEvent(index + 1, index));
    const listClassifierEventPage = vi.fn<OperatorApi["listClassifierEventPage"]>(
      async (request) => {
        const ordered =
          request.order === DescriptorPageOrder.FORWARD
            ? events.filter(
                (event) => Number(event.eventSequence) > Number(request.afterEventSequence),
              )
            : events.toReversed();
        const records = ordered.slice(0, 20);
        return classifierPage(records, ordered.length > records.length ? "more" : "");
      },
    );
    const readJsonDetail = vi.fn<OperatorApi["readJsonDetail"]>(async (token) =>
      classifierInvocation(Number(token.slice("classifier-".length)) - 1),
    );
    const api = createApi({ listClassifierEventPage, readJsonDetail });
    const view = render(
      <Inspector
        api={api}
        run={classifierRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    const table = within(await screen.findByRole("table", { name: "Classifier calls" }));
    const callNumbers = () =>
      table.getAllByRole("button").map((button) => button.getAttribute("aria-label"));
    await waitFor(() =>
      expect(callNumbers()).toEqual(
        Array.from({ length: 25 }, (_, index) => `Call ${index + 1}`),
      ),
    );
    expect(listClassifierEventPage).toHaveBeenCalledTimes(2);
    expect(table.queryByRole("button", { expanded: true })).toBeNull();
    expect(readJsonDetail).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() =>
      expect(callNumbers()).toEqual(
        Array.from({ length: 25 }, (_, index) => `Call ${index + 26}`),
      ),
    );
    expect(listClassifierEventPage).toHaveBeenCalledTimes(3);
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() =>
      expect(callNumbers()).toEqual(
        Array.from({ length: 10 }, (_, index) => `Call ${index + 51}`),
      ),
    );
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    view.rerender(
      <Inspector
        api={api}
        run={classifierRun}
        nodeId={node.nodeId}
        liveClassifierEvents={[classifierEvent(61, 60)]}
        onClose={() => undefined}
      />,
    );
    expect(callNumbers()).toEqual(
      Array.from({ length: 25 }, (_, index) => `Call ${index + 26}`),
    );
    expect(table.queryByRole("button", { expanded: true })).toBeNull();
    expect(readJsonDetail).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    fireEvent.click(table.getByRole("button", { name: "Call 1" }));
    expect(await table.findByLabelText("Input state")).toHaveTextContent("input-0");
    expect(readJsonDetail).toHaveBeenCalledTimes(1);
    fireEvent.click(table.getByRole("button", { name: "Call 1" }));
    fireEvent.click(table.getByRole("button", { name: "Call 1" }));
    expect(table.getByLabelText("Input state")).toHaveTextContent("input-0");
    expect(readJsonDetail).toHaveBeenCalledTimes(1);
    fireEvent.click(
      within(table.getByLabelText("Invocation classification-2")).getAllByRole("cell")[0],
    );
    expect(await table.findByLabelText("Input state")).toHaveTextContent("input-2");
    expect(readJsonDetail).toHaveBeenCalledTimes(2);
  });

  it("keeps history pageable and terminal evidence authoritative over an older running page", async () => {
    const api = createApi({
      listClassifierEventPage: async (request) =>
        request.pageToken === "events"
          ? classifierPage([classifierEvent(4, 1), classifierEvent(3, 0)], "older")
          : classifierPage([
              classifierEvent(2, 1, "running"),
              classifierEvent(1, 0, "running"),
            ]),
      readJsonDetail: async (token) =>
        token === "classifier-4" ? classifierInvocation(1) : classifierInvocation(0),
    });
    render(
      <Inspector
        api={api}
        run={classifierRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Call 2" }));
    expect(await screen.findByLabelText("Input state")).toHaveTextContent("input-1");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled(),
    );
    expect(screen.getAllByRole("rowgroup", { name: /^Invocation / })).toHaveLength(2);
    const first = screen.getByLabelText("Invocation classification-0");
    fireEvent.click(within(first).getByRole("button", { expanded: false }));
    expect(await screen.findByLabelText("Input state")).toHaveTextContent("input-0");
    const second = screen.getByLabelText("Invocation classification-1");
    fireEvent.click(within(second).getByRole("button", { expanded: false }));
    expect(screen.getByLabelText("Input state")).toHaveTextContent("input-1");
    expect(within(first).getByRole("status", { name: "Invocation status" })).toHaveTextContent(
      "success",
    );
    expect(within(second).getByRole("status", { name: "Invocation status" })).toHaveTextContent(
      "success",
    );
  });

  it("does not infer interruption before a long-running call's terminal record has been paged in", async () => {
    const records: ClassifierEventDescriptorMsg[] = [];
    const bodies = new Map<string, ClassifierInvocation>();
    const append = (
      sequence: number,
      index: number,
      status: ClassifierInvocation["status"],
    ) => {
      const event = classifierEvent(sequence, index, status);
      records.push(event);
      bodies.set(event.bodyToken, classifierInvocation(index, status));
    };
    append(1, 0, "running");
    for (let index = 1; index <= 600; index++) {
      append(index * 2, index, "running");
      append(index * 2 + 1, index, "success");
    }
    append(1202, 0, "success");
    const api = createApi({
      listClassifierEventPage: async (request) => {
        const offset = request.pageToken === "events" ? 0 : Number(request.pageToken);
        const page = records.slice(offset, offset + request.pageSize);
        const nextOffset = offset + page.length;
        return classifierPage(page, nextOffset < records.length ? String(nextOffset) : "");
      },
      readJsonDetail: async (token) => {
        const invocation = bodies.get(token);
        if (!invocation) throw new Error("Unknown classifier detail");
        return invocation;
      },
    });
    const completedRun = RunSnapshotMsg.create({
      ...classifierRun,
      summary: { ...classifierRun.summary, status: "success" },
      nodes: [{ ...node, status: "success" }],
    });
    await act(async () => {
      render(
        <Inspector
          api={api}
          run={completedRun}
          nodeId={node.nodeId}
          onClose={() => undefined}
        />,
      );
    });
    const history = within(screen.getByLabelText("Classifier invocations"));
    const next = history.getByRole("button", { name: "Next page" });
    const first = history.getByLabelText("Invocation classification-0");
    expect(within(first).getByRole("status", { name: "Invocation status" })).toHaveTextContent(
      /unknown/i,
    );
    expect(history.queryByLabelText("Input state")).toBeNull();
    for (let page = 0; page < 40 && !next.hasAttribute("disabled"); page++) {
      await act(async () => {
        fireEvent.click(next);
      });
    }
    expect(next).toBeDisabled();
    expect(history.getByLabelText("Invocation classification-600")).toBeVisible();
    const previous = history.getByRole("button", { name: "Previous page" });
    for (let page = 0; page < 40 && !previous.hasAttribute("disabled"); page++) {
      await act(async () => {
        fireEvent.click(previous);
      });
    }
    const completed = history.getByLabelText("Invocation classification-0");
    expect(
      within(completed).getByRole("status", { name: "Invocation status" }),
    ).toHaveTextContent("success");
    fireEvent.click(within(completed).getByRole("button", { expanded: false }));
    expect(await within(completed).findByLabelText("Classification answers")).toBeVisible();
    await act(async () => {
      fireEvent.click(history.getByRole("button", { name: "Return to first invocations" }));
    });
    expect(history.queryByLabelText("Input state")).toBeNull();
    expect(history.getByLabelText("Invocation classification-0")).toBeVisible();
  }, 15_000);

  it("bounds live descriptors while keeping calls in ascending index order", async () => {
    const live = Array.from({ length: 510 }, (_, index) =>
      ClassifierEventDescriptorMsg.create({
        ...classifierEvent(index + 1, index),
        bodyToken: "",
      }),
    );
    await act(async () => {
      render(
        <Inspector
          api={createApi()}
          run={classifierRun}
          nodeId={node.nodeId}
          liveClassifierEvents={live}
          onClose={() => undefined}
        />,
      );
    });
    const history = within(screen.getByLabelText("Classifier invocations"));
    expect(history.getAllByRole("rowgroup", { name: /^Invocation / })).toHaveLength(25);
    expect(history.getByLabelText("Invocation classification-0")).toBeVisible();
    expect(history.queryByLabelText("Invocation classification-509")).toBeNull();
    const nextPage = history.getByRole("button", { name: "Next page" });
    for (let page = 1; page < 20; page++) {
      await act(async () => {
        fireEvent.click(nextPage);
      });
    }
    expect(history.getByLabelText("Invocation classification-499")).toBeVisible();
    expect(history.queryByLabelText("Invocation classification-509")).toBeNull();
    expect(nextPage).toBeDisabled();
  });

  it("pages summaries without collapsed detail requests and reloads evicted calls on expansion", async () => {
    const events = Array.from({ length: 12 }, (_, index) =>
      ClassifierEventDescriptorMsg.create({
        ...classifierEvent(index + 1, index),
        answers: [
          { questionId: "category", type: "choice", choice: "keep" },
          { questionId: "approved", type: "noul", noul: 0 },
          { questionId: "quality", type: "score", score: 0 },
        ],
      }),
    );
    const listClassifierEventPage = vi.fn<OperatorApi["listClassifierEventPage"]>(
      async (request) =>
        request.pageToken === "events"
          ? classifierPage(events.slice(0, 6), "later")
          : classifierPage(events.slice(6)),
    );
    const readJsonDetail = vi.fn<OperatorApi["readJsonDetail"]>(async (token) => {
      const invocation = classifierInvocation(Number(token.slice("classifier-".length)) - 1);
      if (invocation.result) {
        invocation.result.answers.approved = { type: "noul", noul: 0 };
        invocation.result.answers.quality = {
          type: "score",
          score: 0,
          legend: { "0": "Low", "1": "High" },
          probabilities: { "0": 1, "1": 0 },
          confidence: 1,
        };
      }
      return invocation;
    });
    render(
      <Inspector
        api={createApi({ listClassifierEventPage, readJsonDetail })}
        run={classifierRun}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    await screen.findByLabelText("Invocation classification-11");
    expect(listClassifierEventPage).toHaveBeenCalledTimes(2);
    expect(readJsonDetail).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Input state")).toBeNull();
    for (const event of events) {
      const row = screen.getByLabelText(`Invocation ${event.invocationId}`);
      const button = within(row).getByRole("button", { expanded: false });
      expect(button).toHaveAccessibleName(`Call ${event.invocationIndex + 1}`);
      expect(row).toHaveTextContent("category: keep · approved: 0 · quality: 0");
      expect(row).not.toHaveTextContent(/input-/);
    }
    for (let index = 0; index < 9; index++) {
      const row = screen.getByLabelText(`Invocation classification-${index}`);
      fireEvent.click(within(row).getByRole("button", { expanded: false }));
      expect(await within(row).findByLabelText("Input state")).toHaveTextContent(
        `input-${index}`,
      );
    }
    const first = screen.getByLabelText("Invocation classification-0");
    fireEvent.click(within(first).getByRole("button", { expanded: false }));
    expect(await within(first).findByLabelText("Input state")).toHaveTextContent("input-0");
    expect(
      readJsonDetail.mock.calls.filter(([token]) => token === "classifier-1"),
    ).toHaveLength(2);
    expect(readJsonDetail).toHaveBeenCalledTimes(10);
    for (const event of events) {
      const row = screen.getByLabelText(`Invocation ${event.invocationId}`);
      const button = within(row).getByRole("button", { expanded: event.invocationIndex === 0 });
      expect(button).toHaveAccessibleName(`Call ${event.invocationIndex + 1}`);
      expect(within(row).getAllByRole("cell")[0]).toHaveTextContent(
        "category: keep · approved: 0 · quality: 0",
      );
    }
  });

  it("keeps the selected full detail when another expanded call finishes after selection changes", async () => {
    const pending = Promise.withResolvers<unknown>();
    const readJsonDetail = vi.fn<OperatorApi["readJsonDetail"]>((token) =>
      token === "classifier-1" ? pending.promise : Promise.resolve(classifierInvocation(1)),
    );
    const events = [classifierEvent(1, 0), classifierEvent(2, 1)].map((event) =>
      ClassifierEventDescriptorMsg.create({
        ...event,
        sizeBytes: String(DETAIL_CACHE_MAX_BYTES / 2 + 1),
      }),
    );
    render(
      <Inspector
        api={createApi({ readJsonDetail })}
        run={classifierRun}
        nodeId={node.nodeId}
        liveClassifierEvents={events}
        onClose={() => undefined}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Call 2" }));
    expect(await screen.findByLabelText("Input state")).toHaveTextContent("input-1");
    const first = screen.getByLabelText("Invocation classification-0");
    const second = screen.getByLabelText("Invocation classification-1");
    fireEvent.click(within(first).getByRole("button", { expanded: false }));
    fireEvent.click(within(second).getByRole("button", { expanded: false }));
    await act(async () => {
      pending.resolve(classifierInvocation(0));
      await pending.promise;
    });
    expect(within(second).getByLabelText("Input state")).toHaveTextContent("input-1");
    expect(readJsonDetail).toHaveBeenCalledTimes(2);
  });

  it("automatically reloads expanded details evicted by the aggregate byte limit", async () => {
    const readJsonDetail = vi.fn<OperatorApi["readJsonDetail"]>(async (token) =>
      classifierInvocation(token === "classifier-1" ? 0 : 1),
    );
    const api = createApi({ readJsonDetail });
    const first = ClassifierEventDescriptorMsg.create({
      ...classifierEvent(1, 0),
      sizeBytes: String(DETAIL_CACHE_MAX_BYTES / 2 + 1),
    });
    const second = ClassifierEventDescriptorMsg.create({
      ...classifierEvent(2, 1),
      sizeBytes: String(DETAIL_CACHE_MAX_BYTES / 2 + 1),
    });
    const view = render(
      <Inspector
        api={api}
        run={classifierRun}
        nodeId={node.nodeId}
        liveClassifierEvents={[first]}
        onClose={() => undefined}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Call 1" }));
    expect(await screen.findByLabelText("Input state")).toHaveTextContent("input-0");
    view.rerender(
      <Inspector
        api={api}
        run={classifierRun}
        nodeId={node.nodeId}
        liveClassifierEvents={[first, second]}
        onClose={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Call 2" }));
    expect(await screen.findByLabelText("Input state")).toHaveTextContent("input-1");
    const firstCall = screen.getByRole("rowgroup", { name: "Invocation classification-0" });
    fireEvent.click(within(firstCall).getByRole("button", { expanded: false }));
    expect(await screen.findByLabelText("Input state")).toHaveTextContent("input-0");
    expect(screen.getByLabelText("Input state")).not.toHaveTextContent("input-1");
    expect(readJsonDetail).toHaveBeenCalledTimes(3);
  });
});
