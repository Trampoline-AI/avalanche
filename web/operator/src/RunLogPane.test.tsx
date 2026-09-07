import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { LogDescriptorPage } from "./api";
import { LogRecordDescriptorMsg, RunSnapshotMsg } from "./model";
import { RunLogPane } from "./RunLogPane";
import { createApi, node, snapshotFor } from "./test/fixtures";

const run = RunSnapshotMsg.create({
  ...snapshotFor(),
  nodes: [node, { ...node, nodeId: "validate", name: "Validate" }],
  logPageToken: "logs",
  topology: {
    nodeIds: ["fetch", "validate"],
    graph: { fetch: { children: ["validate"] }, validate: { children: [] } },
    nodeTypes: { fetch: "source", validate: "step" },
    displayNames: { fetch: "Fetch", validate: "Validate" },
  },
});
function log(sequence: number, nodeId = "fetch", bodyToken = `log-${sequence}`) {
  return LogRecordDescriptorMsg.create({
    sequence: String(sequence),
    timestamp: sequence,
    nodeId,
    bodyToken,
    sizeBytes: "128",
    level: "info",
  });
}
function page(records: LogRecordDescriptorMsg[], nextPageToken = ""): LogDescriptorPage {
  return {
    operatorInstanceId: run.operatorInstanceId,
    asOfEventUlid: run.asOfEventUlid,
    records,
    nextPageToken,
    nextCursor: records.at(-1)?.sequence ?? "0",
  };
}

describe("run logs", () => {
  it("merges retained and live logs once in sequence order, renders hostile text safely, and filters by node", async () => {
    const bodies: Record<string, string> = {
      "log-1": "Fetched orders",
      "log-2": "Obsolete validation",
      "live-2": "\u001b[32mValidated orders\u001b[0m",
      "log-3": '<img src=x onerror="alert(1)">',
    };
    const api = createApi({
      listLogPage: async (request) =>
        page(
          [log(2, "validate"), log(1)].filter(
            (record) => !request.nodeId || request.nodeId === record.nodeId,
          ),
        ),
      readTextDetail: async (token) => bodies[token],
    });
    const liveLogs = [log(3), log(2, "validate", "live-2")];
    const view = render(
      <RunLogPane api={api} run={run} liveLogs={liveLogs} onSelectNode={() => undefined} />,
    );
    await waitFor(() =>
      expect(
        screen.getAllByRole("article").map((row) => row.querySelector("pre")?.textContent),
      ).toEqual(["Fetched orders", "Validated orders", bodies["log-3"]]),
    );
    expect(view.container.querySelector("img")).toBeNull();
    expect(view.container.textContent).not.toContain("\u001b[");
    view.rerender(
      <RunLogPane
        api={api}
        run={run}
        nodeId="validate"
        liveLogs={liveLogs}
        onSelectNode={() => undefined}
      />,
    );
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(1));
    expect(screen.getByText("Validated orders")).toBeInTheDocument();
    expect(screen.queryByText("Fetched orders")).not.toBeInTheDocument();
  });

  it("preserves the reader's position across older pages and resumes following only on request", async () => {
    const older = Promise.withResolvers<LogDescriptorPage>();
    const api = createApi({
      listLogPage: async (request) =>
        request.pageToken === "older" ? older.promise : page([log(3)], "older"),
      readTextDetail: async (token) => `body-${token}`,
    });
    const view = render(<RunLogPane api={api} run={run} onSelectNode={() => undefined} />);
    await screen.findByText("body-log-3");
    const scroll = view.container.querySelector<HTMLElement>(".run-log-scroll")!;
    let scrollHeight = 1000;
    Object.defineProperties(scroll, {
      scrollHeight: { configurable: true, get: () => scrollHeight },
      clientHeight: { configurable: true, value: 200 },
      scrollTop: { configurable: true, writable: true, value: 500 },
    });
    fireEvent.scroll(scroll);
    const following = screen.getByRole("button", { name: "Auto-scroll logs" });
    expect(following).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(screen.getByRole("button", { name: "Load older logs" }));
    scrollHeight = 1200;
    await act(async () => {
      older.resolve(page([log(2, "validate"), log(1)]));
      await older.promise;
    });
    await waitFor(() => expect(scroll.scrollTop).toBe(700));
    view.rerender(
      <RunLogPane api={api} run={run} liveLogs={[log(4)]} onSelectNode={() => undefined} />,
    );
    await screen.findByText("body-log-4");
    expect(scroll.scrollTop).toBe(700);
    fireEvent.click(following);
    expect(scroll.scrollTop).toBe(1200);
    expect(following).toHaveAttribute("aria-pressed", "true");
  });

  it("aborts collapsed decoding and never displays a late body from an obsolete node scope", async () => {
    const oldBody = Promise.withResolvers<string>();
    const signals: AbortSignal[] = [];
    const api = createApi({
      listLogPage: async (request) => page([log(1, request.nodeId, request.nodeId)]),
      readTextDetail: (token, signal) => {
        if (token === "fetch") {
          signals.push(signal!);
          return oldBody.promise;
        }
        return Promise.resolve("Fresh validation output");
      },
    });
    const view = render(
      <RunLogPane api={api} run={run} nodeId="fetch" onSelectNode={() => undefined} />,
    );
    await waitFor(() => expect(signals.length).toBeGreaterThan(0));
    const pane = screen.getByRole("region", { name: "Run logs" });
    fireEvent.click(within(pane).getByRole("button", { name: /Logs.*Fetch/i }));
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    fireEvent.click(within(pane).getByRole("button", { name: /Logs.*Fetch/i }));
    view.rerender(
      <RunLogPane api={api} run={run} nodeId="validate" onSelectNode={() => undefined} />,
    );
    await screen.findByText("Fresh validation output");
    await act(async () => {
      oldBody.resolve("Obsolete fetched data");
      await oldBody.promise;
    });
    expect(screen.queryByText("Obsolete fetched data")).not.toBeInTheDocument();
    expect(screen.getByText("Fresh validation output")).toBeInTheDocument();
  });
});
