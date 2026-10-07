import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { OperatorApi } from "./api";
import { EvaluationPanel } from "./EvaluationPanel";
import { mapEvaluationRecord, type EvaluationRecord } from "./evaluations";
import { Inspector } from "./Inspector";
import { RunSnapshotMsg } from "./model";
import { EvaluationRecordV2 } from "./generated/operator";
import { createApi, node, snapshotFor } from "./test/fixtures";
import { useRunEvaluations } from "./useRunEvaluations";

function PollingPanel(props: {
  api: OperatorApi;
  operatorInstanceId: string;
  runId: string;
  nodeId: string;
}) {
  const state = useRunEvaluations(props.api, props.operatorInstanceId, props.runId);
  return (
    <EvaluationPanel
      record={state.records[props.nodeId]}
      loading={state.loading}
      error={state.error}
    />
  );
}

function PollingInspector(props: Parameters<typeof Inspector>[0]) {
  const state = useRunEvaluations(
    props.api,
    props.run?.operatorInstanceId ?? "",
    props.run?.summary?.runId,
  );
  return <Inspector {...props} evaluationState={state} />;
}

const pending: EvaluationRecord = {
  evaluationId: "eval-1",
  runId: "run-1",
  nodeId: node.nodeId,
  createdAt: 1,
  status: "pending",
};
const completed: EvaluationRecord = {
  ...pending,
  status: "completed",
  endedAt: 2,
  result: {
    classification: {
      model: "jev-latest",
      usage: { input_tokens: 10, output_tokens: 5 },
      answers: {
        grounded: { type: "noul", noul: 0.9 },
        verdict: {
          type: "choice",
          choice: "pass",
          probabilities: { pass: 0.8, fail: 0.2 },
          confidence: 0.7,
        },
        quality: {
          type: "score",
          score: 1.57,
          legend: { "0": "poor", "1": "fair", "2": "good" },
          probabilities: { "0": 0.01, "1": 0.42, "2": 0.57 },
          confidence: 0.6,
        },
      },
    },
    composites: { overall: 0.75 },
  },
};

async function tick() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500);
  });
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("execution evaluations", () => {
  it("receives late pending and completed results without reopening a terminal agent run", async () => {
    vi.useFakeTimers();
    const listRunEvaluations = vi
      .fn<OperatorApi["listRunEvaluations"]>()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([pending])
      .mockResolvedValue([
        mapEvaluationRecord(
          EvaluationRecordV2.create({
            evaluationId: completed.evaluationId,
            runId: completed.runId,
            nodeId: completed.nodeId,
            createdAt: completed.createdAt,
            endedAt: completed.endedAt,
            status: completed.status,
            resultJson: JSON.stringify(completed.result),
          }),
        ),
      ]);
    const api = createApi({ listRunEvaluations });
    const run = RunSnapshotMsg.create({
      ...snapshotFor(),
      summary: { ...snapshotFor().summary, status: "success" },
      nodes: [{ ...node, status: "success" }],
      topology: {
        ...snapshotFor().topology,
        agentFieldSchemasJson: { [node.nodeId]: JSON.stringify({ inputs: [], outputs: [] }) },
      },
    });
    await act(async () => {
      render(
        <PollingInspector api={api} run={run} nodeId={node.nodeId} onClose={() => undefined} />,
      );
    });
    // The independent poll is alive even while the trace tab is selected.
    await tick();
    fireEvent.click(screen.getByRole("tab", { name: "Evaluations" }));
    expect(screen.getByRole("status")).toBeInTheDocument();
    await tick();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    const panel = screen.getByRole("region", { name: "Execution evaluations" });
    expect(within(panel).queryByRole("article")).not.toBeInTheDocument();
    expect(
      within(panel).queryByRole("heading", { name: "Evaluations" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "grounded probability of true" })).toHaveAttribute(
      "aria-valuenow",
      "0.9",
    );
    const choice = screen.getByRole("region", { name: "Metric verdict" });
    expect(
      within(choice).getByRole("list", { name: "verdict probabilities" }),
    ).toHaveTextContent("80%");
    expect(choice).toHaveTextContent("70%");
    const score = screen.getByRole("region", { name: "Metric quality" });
    expect(score).toHaveTextContent("1.57");
    expect(
      within(score).getByRole("list", { name: "quality level probabilities" }),
    ).toHaveTextContent("57%");
    expect(screen.getByRole("region", { name: "Composites" })).toHaveTextContent("75.0%");
    expect(
      screen.getByRole("complementary", { name: "Run inspector" }).querySelector("header"),
    ).toHaveTextContent("success");
  });

  it("isolates evaluation failures and polling errors, then recovers on the next poll", async () => {
    vi.useFakeTimers();
    const failed: EvaluationRecord = {
      ...pending,
      status: "failed",
      endedAt: 2,
      error: "Judge unavailable",
    };
    const listRunEvaluations = vi
      .fn<OperatorApi["listRunEvaluations"]>()
      .mockResolvedValueOnce([failed])
      .mockRejectedValueOnce(new Error("Connection interrupted"))
      .mockResolvedValue([completed]);
    await act(async () => {
      render(
        <PollingPanel
          api={createApi({ listRunEvaluations })}
          operatorInstanceId="operator-1"
          runId="run-1"
          nodeId={node.nodeId}
        />,
      );
    });
    expect(screen.getByRole("alert")).toHaveTextContent(failed.error);
    await tick();
    expect(screen.getAllByRole("alert")).toHaveLength(2);
    await tick();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Metric grounded" })).toBeInTheDocument();
  });

  it.each(["run", "node", "operator", "api"] as const)(
    "discards an old in-flight result when the %s selection changes",
    async (change) => {
      const old = Promise.withResolvers<EvaluationRecord[]>();
      let oldSignal: AbortSignal | undefined;
      const firstApi = createApi({
        listRunEvaluations: (_run, signal) => {
          oldSignal = signal;
          return old.promise;
        },
      });
      const props = {
        api: firstApi,
        operatorInstanceId: "operator-1",
        runId: "run-1",
        nodeId: node.nodeId,
      };
      const view = render(<PollingPanel {...props} />);
      const replacement = createApi({ listRunEvaluations: async () => [] });
      const next = {
        ...props,
        api: change === "api" ? replacement : firstApi,
        runId: change === "run" ? "run-2" : props.runId,
        nodeId: change === "node" ? "other" : props.nodeId,
        operatorInstanceId: change === "operator" ? "operator-2" : props.operatorInstanceId,
      };
      if (change !== "api" && change !== "node") firstApi.listRunEvaluations = async () => [];
      await act(async () => {
        view.rerender(<PollingPanel {...next} />);
      });
      expect(oldSignal?.aborted).toBe(change !== "node");
      await act(async () => {
        old.resolve([completed]);
      });
      expect(screen.queryByRole("region", { name: "Metric grounded" })).not.toBeInTheDocument();
      expect(screen.queryByRole("article")).not.toBeInTheDocument();
    },
  );
});
