import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getTotalSize: () => count * 48,
    getVirtualItems: () =>
      Array.from({ length: count }, (_, index) => ({
        index,
        size: 48,
        start: index * 48,
      })),
    measureElement: () => undefined,
  }),
}));

import type { LogDescriptorPage, OperatorApi } from "./api";
import { DETAIL_CACHE_MAX_BYTES } from "./detailProjection";
import {
  CatalogSnapshotMsg,
  DescriptorPageOrder,
  LogRecordDescriptorMsg,
  RunSnapshotMsg,
} from "./model";
import { RunLogPane } from "./RunLogPane";

const run = RunSnapshotMsg.create({
  operatorInstanceId: "operator-1",
  asOfSequence: "9",
  summary: {
    runId: "run-1",
    workflowId: "flow.py::demo",
    workflowDisplayName: "demo",
    status: "success",
  },
  nodes: [
    { nodeId: "fetch_1", name: "fetch", nodeType: "source", status: "success" },
    { nodeId: "validate_1", name: "validate", nodeType: "step", status: "success" },
  ],
  logPageToken: "logs",
  topology: {
    nodeIds: ["fetch_1", "validate_1"],
    graph: { fetch_1: { children: ["validate_1"] }, validate_1: { children: [] } },
    nodeTypes: { fetch_1: "source", validate_1: "step" },
    displayNames: { fetch_1: "Fetch", validate_1: "Validate" },
  },
});

function log(sequence: number, nodeId: string, bodyToken = `log-${sequence}`) {
  return LogRecordDescriptorMsg.create({
    sequence: String(sequence),
    timestamp: 1_700_000_000 + sequence,
    level: "INFO",
    nodeId,
    sizeBytes: "16",
    bodyToken,
  });
}

function page(
  records: LogRecordDescriptorMsg[],
  nextPageToken = "",
  nextCursor = records[0]?.sequence ?? "0",
): LogDescriptorPage {
  return {
    operatorInstanceId: run.operatorInstanceId,
    asOfSequence: run.asOfSequence,
    records,
    nextPageToken,
    nextCursor,
  };
}

function operatorApi(overrides: Partial<OperatorApi> = {}): OperatorApi {
  const defaults: OperatorApi = {
    getCatalog: async () => CatalogSnapshotMsg.create(),
    loadBaseline: async () => ({
      catalog: CatalogSnapshotMsg.create(),
      asOfSequence: "0",
      runs: [],
    }),
    getLatestRunSnapshot: async () => run,
    streamUpdates: async function* () {
      return;
    },
    listLogPage: async () => page([]),
    listAgentEventPage: async () => ({
      operatorInstanceId: run.operatorInstanceId,
      asOfSequence: run.asOfSequence,
      runId: "run-1",
      nodeId: "fetch_1",
      records: [],
      nextPageToken: "",
      nextCursor: "0",
    }),
    readJsonDetail: async () => undefined,
    readTextDetail: async (token) => `body-${token}`,
    startRun: async () => "run-1",
    cancelRun: async () => undefined,
  };
  return { ...defaults, ...overrides };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("RunLogPane", () => {
  it("merges retained and live records into one ordered cross-step stream", async () => {
    const listLogPage = vi.fn<OperatorApi["listLogPage"]>(async () =>
      page([log(2, "validate_1"), log(1, "fetch_1")]),
    );
    const readTextDetail = vi.fn(async (token: string) => `body-${token}`);
    const onSelectNode = vi.fn();
    render(
      <RunLogPane
        api={operatorApi({ listLogPage, readTextDetail })}
        run={run}
        liveLogs={[log(3, "fetch_1"), log(2, "validate_1", "live-2")]}
        onSelectNode={onSelectNode}
      />,
    );

    const pane = screen.getByRole("region", { name: "Run logs" });
    await waitFor(() => expect(within(pane).getAllByRole("article")).toHaveLength(3));
    await waitFor(() => expect(pane).toHaveTextContent("body-live-2"));
    const rows = within(pane).getAllByRole("article");
    expect(rows.map((row) => row.querySelector("pre")?.textContent)).toEqual([
      "body-log-1",
      "body-live-2",
      "body-log-3",
    ]);
    expect(listLogPage).toHaveBeenCalledWith(
      expect.objectContaining({
        pageToken: "logs",
        nodeId: "",
        order: DescriptorPageOrder.NEWEST_FIRST,
      }),
      expect.any(AbortSignal),
    );

    fireEvent.click(within(rows[0]).getByRole("button", { name: /Fetch/ }));
    expect(onSelectNode).toHaveBeenCalledWith("fetch_1");
    expect(within(pane).queryByText("Start of retained logs")).not.toBeInTheDocument();
  });

  it("renders ANSI styles in hydrated log bodies", async () => {
    render(
      <RunLogPane
        api={operatorApi({
          listLogPage: async () => page([log(1, "fetch")]),
          readTextDetail: async () => "\u001B[1;31mfailed\u001B[0m",
        })}
        run={run}
        onSelectNode={() => undefined}
      />,
    );

    const pane = screen.getByRole("region", { name: "Run logs" });
    const body = await within(pane).findByText("failed");
    expect(body).toHaveStyle({ color: "rgb(196, 61, 54)", fontWeight: "700" });
    expect(within(pane).getByRole("button", { name: /Fetch/ })).toBeInTheDocument();
  });

  it("uses the canonical graph node ID for the log filter and cancels obsolete scope work", async () => {
    const requestSignals: AbortSignal[] = [];
    const listLogPage = vi.fn<OperatorApi["listLogPage"]>(async (request, signal) => {
      if (signal) requestSignals.push(signal);
      return request.nodeId === "fetch_1"
        ? page([log(1, "fetch_1")])
        : page([log(1, "fetch_1"), log(2, "validate_1")]);
    });
    const api = operatorApi({ listLogPage });
    const view = render(
      <RunLogPane
        api={api}
        run={run}
        nodeId="fetch_1"
        liveLogs={[log(3, "fetch_1"), log(4, "validate_1")]}
        onSelectNode={() => undefined}
      />,
    );

    expect(await screen.findByText("Fetch · fetch_1")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));
    expect(listLogPage).toHaveBeenLastCalledWith(
      expect.objectContaining({ nodeId: "fetch_1" }),
      expect.any(AbortSignal),
    );

    view.rerender(
      <RunLogPane
        api={api}
        run={run}
        liveLogs={[log(3, "fetch_1"), log(4, "validate_1")]}
        onSelectNode={() => undefined}
      />,
    );
    await waitFor(() => expect(listLogPage).toHaveBeenCalledTimes(2));
    expect(requestSignals[0].aborted).toBe(true);
    expect(await screen.findByText("All steps")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(4));
  });

  it("preserves older-page position and explicitly controls auto-scroll to new output", async () => {
    const olderPage = deferred<LogDescriptorPage>();
    const listLogPage = vi.fn<OperatorApi["listLogPage"]>(async (request) => {
      if (request.pageToken === "older") return olderPage.promise;
      return page([log(3, "fetch")], "older", "3");
    });
    const api = operatorApi({ listLogPage });
    const view = render(<RunLogPane api={api} run={run} onSelectNode={() => undefined} />);

    const loadOlder = await screen.findByRole("button", { name: "Load older logs" });
    await screen.findByText("body-log-3");
    const autoScroll = screen.getByRole("button", { name: "Auto-scroll logs" });
    expect(autoScroll).toHaveAttribute("aria-pressed", "true");
    expect(autoScroll).toHaveTextContent("Auto-scroll on");
    const scroll = view.container.querySelector<HTMLElement>(".run-log-scroll");
    expect(scroll).not.toBeNull();
    let scrollHeight = 1_000;
    Object.defineProperties(scroll!, {
      scrollHeight: { configurable: true, get: () => scrollHeight },
      clientHeight: { configurable: true, value: 200 },
      scrollTop: { configurable: true, writable: true, value: 500 },
    });
    fireEvent.scroll(scroll!);
    expect(autoScroll).toHaveAttribute("aria-pressed", "false");
    expect(autoScroll).toHaveTextContent("Auto-scroll off");

    fireEvent.click(loadOlder);
    expect(listLogPage).toHaveBeenLastCalledWith(
      expect.objectContaining({
        pageToken: "older",
        beforeSequence: "3",
        nodeId: "",
        order: DescriptorPageOrder.NEWEST_FIRST,
      }),
      expect.any(AbortSignal),
    );
    scrollHeight = 1_200;
    await act(async () => {
      olderPage.resolve(page([log(1, "fetch"), log(2, "validate")]));
      await olderPage.promise;
    });
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(3));
    expect(scroll!.scrollTop).toBe(700);

    view.rerender(
      <RunLogPane
        api={api}
        run={run}
        liveLogs={[log(4, "validate")]}
        onSelectNode={() => undefined}
      />,
    );
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(4));
    expect(scroll!.scrollTop).toBe(700);
    fireEvent.click(autoScroll);
    expect(autoScroll).toHaveAttribute("aria-pressed", "true");
    expect(autoScroll).toHaveTextContent("Auto-scroll on");
    expect(scroll!.scrollTop).toBe(1_200);

    scrollHeight = 1_400;
    view.rerender(
      <RunLogPane
        api={api}
        run={run}
        liveLogs={[log(4, "validate"), log(5, "fetch")]}
        onSelectNode={() => undefined}
      />,
    );
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(5));
    expect(scroll!.scrollTop).toBe(1_400);
    fireEvent.click(autoScroll);
    expect(autoScroll).toHaveAttribute("aria-pressed", "false");
  });

  it("resizes vertically from an accessible horizontal drag divider", () => {
    render(<RunLogPane api={operatorApi()} run={run} onSelectNode={() => undefined} />);
    const pane = screen.getByRole("region", { name: "Run logs" });
    Object.defineProperty(pane.parentElement, "clientHeight", {
      configurable: true,
      value: 800,
    });
    const divider = screen.getByRole("separator", { name: "Resize logs" });
    expect(divider).toHaveAttribute("aria-orientation", "horizontal");
    expect(divider).toHaveAttribute("aria-valuenow", "260");

    fireEvent.pointerDown(divider, { pointerId: 1, clientY: 400 });
    fireEvent.pointerMove(divider, { pointerId: 1, clientY: 320 });
    expect(divider).toHaveAttribute("aria-valuenow", "340");
    expect(pane).toHaveStyle({ flexBasis: "340px" });

    fireEvent.pointerMove(divider, { pointerId: 1, clientY: 700 });
    fireEvent.pointerUp(divider, { pointerId: 1, clientY: 700 });
    expect(divider).toHaveAttribute("aria-valuenow", "140");
    fireEvent.keyDown(divider, { key: "ArrowUp" });
    expect(divider).toHaveAttribute("aria-valuenow", "156");
    fireEvent.keyDown(divider, { key: "End" });
    expect(divider).toHaveAttribute("aria-valuenow", "600");

    fireEvent.click(screen.getByRole("button", { name: /logsall steps/i }));
    expect(screen.queryByRole("separator", { name: "Resize logs" })).not.toBeInTheDocument();
  });

  it("bounds text decoding and reports an oversized record", async () => {
    const oversized = {
      ...log(1, "fetch"),
      sizeBytes: String(DETAIL_CACHE_MAX_BYTES + 1),
    };
    render(
      <RunLogPane
        api={operatorApi({
          listLogPage: async () => page([oversized]),
          readTextDetail: async () => "small body with an oversized declared cost",
        })}
        run={run}
        onSelectNode={() => undefined}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A log record exceeds the browser detail limit.",
    );
    expect(screen.getByText("[log body omitted]")).toBeInTheDocument();
  });

  it("aborts an active decode batch when the pane collapses", async () => {
    const pendingBody = deferred<string>();
    const decodeSignals: AbortSignal[] = [];
    const readTextDetail = vi.fn((_token: string, signal?: AbortSignal) => {
      if (signal) decodeSignals.push(signal);
      return pendingBody.promise;
    });
    render(
      <RunLogPane
        api={operatorApi({
          listLogPage: async () =>
            page(Array.from({ length: 25 }, (_, index) => log(index + 1, "fetch"))),
          readTextDetail,
        })}
        run={run}
        onSelectNode={() => undefined}
      />,
    );

    await waitFor(() => expect(readTextDetail).toHaveBeenCalledTimes(20));
    fireEvent.click(screen.getByRole("button", { name: /logsall steps/i }));
    expect(screen.getByRole("button", { name: /logsall steps/i })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(decodeSignals).toHaveLength(20);
    expect(decodeSignals.every((signal) => signal.aborted)).toBe(true);
    expect(screen.queryByRole("status", { name: /Decoding/ })).not.toBeInTheDocument();
  });
});
