import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AgentEventDescriptorPage, OperatorApi } from "./api";
import { Inspector } from "./Inspector";
import { AgentEventDescriptorMsg, DescriptorPageOrder, RunSnapshotMsg } from "./model";
import { createApi, node, snapshotFor } from "./test/fixtures";

const run = RunSnapshotMsg.create({
  ...snapshotFor(),
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

describe("retained run inspection", () => {
  it("cancels inactive hydration, keeps fresh output after a late input completes, and exposes page failures", async () => {
    const input = Promise.withResolvers<unknown>();
    const output = Promise.withResolvers<unknown>();
    let inputSignal: AbortSignal | undefined;
    const api = createApi({
      listAgentEventPage: async (request) => {
        if (request.pageToken === "broken") throw new Error("Retained page unavailable");
        return eventPage([event(1, "run.started"), event(2, "run.succeeded")]);
      },
      readJsonDetail: (token, signal) => {
        if (token === "event-1") {
          inputSignal = signal;
          return input.promise;
        }
        return output.promise;
      },
    });
    const view = render(
      <Inspector api={api} run={run} nodeId={node.nodeId} onClose={() => undefined} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "inputs" }));
    await waitFor(() => expect(inputSignal).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: "output" }));
    expect(inputSignal?.aborted).toBe(true);
    await act(async () => {
      output.resolve({ outputs: { answer: "fresh output" } });
      await output.promise;
    });
    expect(await screen.findByText("fresh output")).toBeInTheDocument();
    await act(async () => {
      input.resolve({ inputs: { question: "stale input" } });
      await input.promise;
    });
    expect(screen.queryByText("stale input")).not.toBeInTheDocument();
    expect(screen.getByText("fresh output")).toBeInTheDocument();

    view.rerender(
      <Inspector
        api={api}
        run={RunSnapshotMsg.create({
          ...run,
          nodes: [{ ...run.nodes[0], eventPageToken: "broken" }],
        })}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("Retained page unavailable");
  });

  it.each(["inputs", "output"] as const)(
    "preserves the %s root while paging beyond the descriptor window",
    async (tab) => {
      const listAgentEventPage = vi.fn<OperatorApi["listAgentEventPage"]>(async (request) => {
        const pageIndex = request.pageToken === "events" ? 0 : Number(request.pageToken);
        const records = Array.from({ length: 100 }, (_, index) => {
          const offset = pageIndex * 100 + index;
          const sequence =
            request.order === DescriptorPageOrder.FORWARD ? offset + 1 : 600 - offset;
          return event(
            sequence,
            sequence === 1
              ? "run.started"
              : sequence === 600
                ? "run.succeeded"
                : "iteration.recorded",
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
      fireEvent.click(screen.getByRole("button", { name: tab }));
      const value = tab === "inputs" ? "Original input" : "Final output";
      await screen.findByText(value);
      for (let page = 1; page < 6; page += 1) {
        const more = screen.getByRole("button", { name: "Load more events" });
        await waitFor(() => expect(more).toBeEnabled());
        fireEvent.click(more);
        await waitFor(() => expect(listAgentEventPage).toHaveBeenCalledTimes(page + 1));
      }
      await waitFor(() =>
        expect(
          screen.queryByRole("button", { name: "Load more events" }),
        ).not.toBeInTheDocument(),
      );
      expect(screen.getByText(value)).toBeInTheDocument();
    },
  );
});
