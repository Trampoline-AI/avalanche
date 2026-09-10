import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AgentEventDescriptorPage, OperatorApi } from "./api";
import { Inspector } from "./Inspector";
import { AgentEventDescriptorMsg, DescriptorPageOrder, RunSnapshotMsg } from "./model";
import { createApi, node, snapshotFor } from "./test/fixtures";

const run = RunSnapshotMsg.create({
  ...snapshotFor(),
  nodes: [{ ...node, trace: { available: true, header: { model: "retained-model" } } }],
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
  it("shows authored skip details without confusing them with blocked dependencies", () => {
    const api = createApi();
    const skippedRun = RunSnapshotMsg.create({
      ...run,
      nodes: [{
        ...node,
        status: "skipped",
        skip: {
          reason: "No eligible records",
          metadataJson: '{"partition":"today","count":0}',
        },
      }],
    });
    const view = render(
      <Inspector api={api} run={skippedRun} nodeId={node.nodeId} onClose={() => undefined} />,
    );
    expect(screen.getByRole("region", { name: "Skip detail" })).toHaveTextContent(
      "No eligible records",
    );
    expect(screen.getByText("today")).toBeInTheDocument();
    expect(screen.queryByText(/Upstream dependency failed/)).not.toBeInTheDocument();

    view.rerender(
      <Inspector
        api={api}
        run={RunSnapshotMsg.create({
          ...run,
          summary: { ...run.summary, status: "cancelled" },
          nodes: [{ ...node, status: "skipped" }],
        })}
        nodeId={node.nodeId}
        onClose={() => undefined}
      />,
    );
    expect(screen.getByRole("region", { name: "Skip detail" })).toHaveTextContent(
      "Run cancelled.",
    );
    expect(screen.queryByText("No eligible records")).not.toBeInTheDocument();
    expect(screen.queryByText("today")).not.toBeInTheDocument();
  });

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

  it("hydrates turns on demand and keeps expanded content across live appends", async () => {
    const records = Array.from({ length: 5 }, (_, index) => ({
      ...event(index + 1),
      sizeBytes: String(2 * 1024 * 1024),
    }));
    const reads = new Map<string, number>();
    const api = createApi({
      listAgentEventPage: async () => eventPage(records),
      readJsonDetail: async (token) => {
        const version = (reads.get(token) ?? 0) + 1;
        reads.set(token, version);
        return { reasoning: `Retained ${token} version ${version}` };
      },
    });
    const view = render(
      <Inspector api={api} run={run} nodeId={node.nodeId} onClose={() => undefined} />,
    );
    expect(reads.size).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    fireEvent.click(await screen.findByRole("button", { name: "Expand turns" }));
    for (let index = 0; index < 5; index += 1) {
      fireEvent.click(await screen.findByRole("button", { name: `Expand ${index}` }));
      await screen.findByText(`Retained event-${index + 1} version 1`);
    }
    view.rerender(
      <Inspector
        api={api}
        run={run}
        nodeId={node.nodeId}
        liveEvents={[event(6)]}
        onClose={() => undefined}
      />,
    );
    await screen.findByRole("button", { name: "Expand 5" });
    expect(screen.getByText("Retained event-5 version 1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Collapse 0" }));
    fireEvent.click(screen.getByRole("button", { name: "Expand 0" }));
    expect(await screen.findByText("Retained event-1 version 2")).toBeInTheDocument();
  });
});
