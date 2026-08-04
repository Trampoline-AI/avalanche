import type { ComponentType } from "react";

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

interface RenderedNode {
  id: string;
  position: object;
  data: {
    onOpen: () => void;
  };
}
interface RenderedEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string;
  targetHandle?: string;
  data?: {
    lane: number;
  };
  type?: string;
}


const graphMetrics = vi.hoisted(() => ({
  layoutCount: 0,
  renderCount: 0,
  seenPositions: new WeakSet<object>(),
  nodeSets: [] as RenderedNode[][],
  edgeSets: [] as RenderedEdge[][],
}));

vi.mock("@xyflow/react", () => {
  return {
    Background: () => null,
    Controls: () => null,
    Handle: ({
      id,
      className,
      type,
      position,
    }: {
      id: string;
      className?: string;
      type: string;
      position: string;
    }) => (
      <span
        className={className}
        data-testid="graph-handle"
        data-handle-id={id}
        data-handle-type={type}
        data-handle-position={position}
      />
    ),
    BaseEdge: () => null,
    MarkerType: { ArrowClosed: "arrow-closed" },
    Position: { Bottom: "bottom", Left: "left", Right: "right", Top: "top" },
    useStore: () => 0,
    ReactFlow: ({
      nodes,
      edges,
      nodeTypes,
    }: {
      nodes: RenderedNode[];
      edges: RenderedEdge[];
      nodeTypes: Record<string, ComponentType<{ data: unknown }>>;
    }) => {
      graphMetrics.renderCount += 1;
      graphMetrics.nodeSets.push(nodes);
      graphMetrics.edgeSets.push(edges);
      const firstPosition = nodes[0]?.position;
      if (firstPosition && !graphMetrics.seenPositions.has(firstPosition)) {
        graphMetrics.seenPositions.add(firstPosition);
        graphMetrics.layoutCount += 1;
      }
      const NodeComponent = nodeTypes.workflow;
      return (
        <div>
          <span data-testid="edge-count">{edges.length}</span>
          {nodes.map((node) => (
            <NodeComponent key={node.id} data={node.data} />
          ))}
        </div>
      );
    },
  };
});

import { GraphCanvas, parseAgentDeclaration } from "./GraphCanvas";
import { Markdown } from "./Markdown";
import {
  FlowInfoMsg,
  NodeSnapshotMsg,
  TraceDescriptorMsg,
  WorkflowTopologyMsg,
} from "./generated/operator";

function metadata(field: string) {
  return JSON.stringify({
    signature: {
      inputs: [{ name: field, type: "str" }],
      outputs: [],
    },
  });
}

function fieldSchemas(field: string) {
  return JSON.stringify({
    inputs: [{ name: field, type: "str", description: "" }],
    outputs: [],
  });
}

const workflow = FlowInfoMsg.create({
  workflowId: "flow.py::demo",
  displayName: "Current",
  nodeIds: ["agent", "store"],
  graph: {
    agent: { children: ["store", "store"] },
    store: { children: [] },
  },
  nodeTypes: { agent: "step", store: "dest" },
  displayNames: { agent: "Current agent", store: "Store" },
  agentMetadataJson: { agent: metadata("current_input") },
});

describe("GraphCanvas", () => {
  beforeEach(() => {
    graphMetrics.layoutCount = 0;
    graphMetrics.renderCount = 0;
    graphMetrics.seenPositions = new WeakSet<object>();
    graphMetrics.nodeSets = [];
    graphMetrics.edgeSets = [];
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("bounds Markdown source, blocks images, reveals chunks, and resets on source changes", () => {
    const sourceCharacterBudget = 128;
    const source = [
      "# Safe",
      "[blocked](javascript:alert(1))",
      "![tracker](http://127.0.0.1:9/pixel)",
      "A".repeat(sourceCharacterBudget),
      "SECOND_MARKER",
      "B".repeat(sourceCharacterBudget),
      "THIRD_MARKER",
    ].join("\n\n");
    const view = render(
      <Markdown
        className="bounded-markdown"
        sourceCharacterBudget={sourceCharacterBudget}
      >
        {source}
      </Markdown>,
    );

    const markdown = view.container.querySelector(".bounded-markdown");
    expect(markdown?.textContent?.length).toBeLessThanOrEqual(
      sourceCharacterBudget + "Show more".length,
    );
    expect(view.container.querySelector("img")).not.toBeInTheDocument();
    expect(screen.queryByText("SECOND_MARKER")).not.toBeInTheDocument();
    const blockedLink = view.container.querySelector("a");
    expect(blockedLink).toHaveTextContent("blocked");
    expect(blockedLink?.getAttribute("href") ?? "").not.toContain("javascript:");

    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(screen.getByText("SECOND_MARKER")).toBeInTheDocument();
    expect(screen.queryByText("THIRD_MARKER")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(screen.getByText("THIRD_MARKER")).toBeInTheDocument();

    const replacement = `# Replacement\n\n${"R".repeat(sourceCharacterBudget)}\n\nRESET_TAIL`;
    view.rerender(
      <Markdown
        className="bounded-markdown"
        sourceCharacterBudget={sourceCharacterBudget}
      >
        {replacement}
      </Markdown>,
    );
    expect(screen.getByRole("heading", { name: "Replacement" })).toBeInTheDocument();
    expect(screen.queryByText("THIRD_MARKER")).not.toBeInTheDocument();
    expect(screen.queryByText("RESET_TAIL")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show more" })).toBeInTheDocument();
  });

  it("renders the current definition and keeps a historical run on recorded metadata", () => {
    const view = render(
      <GraphCanvas workflow={workflow} onOpenNode={() => undefined} />,
    );

    expect(screen.getByText("Current agent")).toBeInTheDocument();
    expect(screen.getByText("Store")).toBeInTheDocument();
    expect(screen.getByText("current_input")).toBeInTheDocument();
    expect(screen.getByTestId("edge-count")).toHaveTextContent("1");

    view.rerender(
      <GraphCanvas
        workflow={workflow}
        runTopology={WorkflowTopologyMsg.create({
          nodeIds: ["agent"],
          graph: { agent: { children: [] } },
          nodeTypes: { agent: "step" },
          displayNames: { agent: "Recorded agent" },
          agentFieldSchemasJson: { agent: fieldSchemas("recorded_input") },
        })}
        runNodes={[
          NodeSnapshotMsg.create({
            nodeId: "agent",
            name: "Recorded agent",
            nodeType: "step",
            status: "failed",
            error: "recorded failure",
            revision: "3",
          }),
        ]}
        onOpenNode={() => undefined}
      />,
    );

    expect(screen.getByText("Recorded agent")).toBeInTheDocument();
    expect(screen.getByText("recorded_input")).toBeInTheDocument();
    expect(screen.getByText("recorded failure")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect Recorded agent" }).closest(".node-card"))
      .toHaveClass("status-failed");
    expect(screen.queryByText("Store")).not.toBeInTheDocument();
    expect(screen.queryByText("current_input")).not.toBeInTheDocument();
  });

  it("distinguishes repeated invocations by stable node identity", () => {
    render(
      <GraphCanvas
        workflow={FlowInfoMsg.create({
          workflowId: "flow.py::repeated",
          displayName: "Repeated",
          nodeIds: ["repeat_1", "repeat_2"],
          graph: {
            repeat_1: { children: ["repeat_2"] },
            repeat_2: { children: [] },
          },
          nodeTypes: { repeat_1: "step", repeat_2: "step" },
          displayNames: { repeat_1: "repeat", repeat_2: "repeat" },
        })}
        onOpenNode={() => undefined}
      />,
    );

    expect(screen.getByRole("button", { name: "Inspect repeat #1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect repeat #2" })).toBeInTheDocument();
    expect(screen.getByText("#1")).toBeInTheDocument();
    expect(screen.getByText("#2")).toBeInTheDocument();
  });

  it("reuses nodes and layout for detail-only rerenders while applying status and topology changes", () => {
    const onOpenNode = vi.fn();
    const topology = WorkflowTopologyMsg.create({
      nodeIds: ["agent"],
      graph: { agent: { children: [] } },
      nodeTypes: { agent: "step" },
      displayNames: { agent: "Recorded agent" },
    });
    const runningNode = NodeSnapshotMsg.create({
      nodeId: "agent",
      name: "Recorded agent",
      nodeType: "step",
      status: "running",
      startedAt: 1,
    });
    const view = render(
      <GraphCanvas
        runTopology={topology}
        runNodes={[runningNode]}
        onOpenNode={onOpenNode}
      />,
    );
    const initialNodes = graphMetrics.nodeSets.at(-1);
    const initialPosition = initialNodes?.[0].position;
    const initialOpen = initialNodes?.[0].data.onOpen;

    view.rerender(
      <GraphCanvas
        runTopology={topology}
        runNodes={[
          {
            ...runningNode,
            trace: TraceDescriptorMsg.create({ status: "success" }),
          },
        ]}
        onOpenNode={onOpenNode}
      />,
    );

    expect(screen.getByText("running")).toBeInTheDocument();
    expect(graphMetrics.renderCount).toBe(1);
    expect(graphMetrics.layoutCount).toBe(1);
    expect(graphMetrics.nodeSets.at(-1)).toBe(initialNodes);

    view.rerender(
      <GraphCanvas
        runTopology={topology}
        runNodes={[{ ...runningNode, status: "success", endedAt: 2 }]}
        onOpenNode={onOpenNode}
      />,
    );

    expect(screen.getByText("success")).toBeInTheDocument();
    expect(graphMetrics.renderCount).toBe(2);
    expect(graphMetrics.layoutCount).toBe(1);
    expect(graphMetrics.nodeSets.at(-1)).not.toBe(initialNodes);
    expect(graphMetrics.nodeSets.at(-1)?.[0].position).toBe(initialPosition);
    expect(graphMetrics.nodeSets.at(-1)?.[0].data.onOpen).toBe(initialOpen);

    view.rerender(
      <GraphCanvas
        runTopology={{
          ...topology,
          displayNames: { agent: "Renamed agent" },
        }}
        runNodes={[{ ...runningNode, status: "success", endedAt: 2 }]}
        onOpenNode={onOpenNode}
      />,
    );

    expect(screen.getByText("Renamed agent")).toBeInTheDocument();
    expect(graphMetrics.renderCount).toBe(3);
    expect(graphMetrics.layoutCount).toBe(1);
    view.rerender(
      <GraphCanvas
        runTopology={WorkflowTopologyMsg.create({
          ...topology,
          nodeIds: ["agent", "store"],
          graph: {
            agent: { children: ["store"] },
            store: { children: [] },
          },
          displayNames: { agent: "Renamed agent", store: "Store" },
        })}
        runNodes={[{ ...runningNode, status: "success", endedAt: 2 }]}
        onOpenNode={onOpenNode}
      />,
    );

    expect(screen.getByText("Store")).toBeInTheDocument();
    expect(graphMetrics.renderCount).toBe(4);
    expect(graphMetrics.layoutCount).toBe(2);
    fireEvent.click(screen.getByRole("button", { name: "Inspect Renamed agent" }));
    expect(onOpenNode).toHaveBeenCalledWith("agent");
  });

  it("renders typed input and output lists without card instructions", () => {
    const declaration = JSON.stringify({
      signature: {
        instructions:
          "# Triage\n\n- Keep **context**\n- Return a summary\n\n<script>unsafe()</script>\n\n" +
          "x".repeat(500) +
          "\n\nGRAPH_TAIL",
        inputs: [
          {
            name: "record",
            annotation: "Record",
            description: "The record to inspect.",
          },
        ],
        outputs: [
          {
            name: "summary",
            type: "str",
            description: "A concise result.",
          },
        ],
      },
      skills: [
        {
          name: "audit",
          instructions: "Check **every** field.",
          modules: ["ignored"],
        },
      ],
      tools: [
        {
          name: "lookup",
          description: "Look up a record.",
          implementation: { internal: true },
        },
      ],
    });
    const view = render(
      <GraphCanvas
        workflow={FlowInfoMsg.create({
          workflowId: "flow.py::markdown",
          displayName: "Markdown",
          nodeIds: ["agent"],
          graph: { agent: { children: [] } },
          nodeTypes: { agent: "step" },
          displayNames: { agent: "Triage agent" },
          agentMetadataJson: { agent: declaration },
        })}
        onOpenNode={() => undefined}
      />,
    );

    expect(screen.queryByRole("heading", { level: 1, name: "Triage" })).not.toBeInTheDocument();
    expect(screen.queryByText("context")).not.toBeInTheDocument();
    expect(screen.getByText("Record").tagName).toBe("CODE");
    expect(screen.getByText("str").tagName).toBe("CODE");
    const inputRow = screen.getByText("record").closest(".node-field");
    const outputRow = screen.getByText("summary").closest(".node-field");
    expect(inputRow).not.toHaveClass("node-plug-row");
    expect(outputRow).not.toHaveClass("node-plug-row");
    expect(inputRow?.closest(".node-inputs")).toBeInTheDocument();
    expect(outputRow?.closest(".node-outputs")).toBeInTheDocument();
    expect(view.container.querySelector(".node-instruction-excerpt")).not.toBeInTheDocument();
    expect(view.container.querySelector(".node-header > .node-title")).toHaveTextContent(
      "Triage agent",
    );
    expect(view.container).not.toHaveTextContent("[object Object]");
    expect(screen.queryByText("GRAPH_TAIL")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();

    const parsed = parseAgentDeclaration(declaration);
    expect(parsed?.skills).toEqual([
      { name: "audit", instructions: "Check **every** field." },
    ]);
    expect(parsed?.tools).toEqual([
      { name: "lookup", description: "Look up a record." },
    ]);

    const handles = screen.getAllByTestId("graph-handle");
    expect(handles).toHaveLength(4);
    for (const handle of handles) {
      expect(handle).toHaveClass("node-handle");
      expect(handle).not.toHaveClass("endpoint-circle");
    }
    expect(
      handles.find((handle) => handle.getAttribute("data-handle-id") === "target-left"),
    ).not.toHaveClass("node-handle-input");
    expect(
      handles.find((handle) => handle.getAttribute("data-handle-id") === "target-bottom"),
    ).not.toHaveClass("node-handle-input");
    expect(
      handles.find((handle) => handle.getAttribute("data-handle-id") === "source-right"),
    ).not.toHaveClass("node-handle-output");
  });

  it("deduplicates dependencies and selects right or bottom source handles by depth", () => {
    render(
      <GraphCanvas
        workflow={FlowInfoMsg.create({
          workflowId: "flow.py::routing",
          displayName: "Routing",
          nodeIds: ["source", "middle", "target"],
          graph: {
            source: { children: ["middle", "target", "target"] },
            middle: { children: ["target"] },
            target: { children: [] },
          },
          nodeTypes: {
            source: "step",
            middle: "step",
            target: "step",
          },
          displayNames: {
            source: "Source",
            middle: "Middle",
            target: "Target",
          },
        })}
        onOpenNode={() => undefined}
      />,
    );

    expect(screen.getByTestId("edge-count")).toHaveTextContent("3");
    expect(graphMetrics.edgeSets.at(-1)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: "source->middle",
          sourceHandle: "source-right",
          targetHandle: "target-left",
          type: "step",
        }),
        expect.objectContaining({
          id: "middle->target",
          sourceHandle: "source-right",
          targetHandle: "target-left",
          type: "step",
        }),
        expect.objectContaining({
          id: "source->target",
          sourceHandle: "source-bottom",
          targetHandle: "target-bottom",
          type: "skip",
          data: { lane: 0 },
        }),
      ]),
    );
  });

  it("assigns distinct lower routing lanes to skip edges", () => {
    render(
      <GraphCanvas
        workflow={FlowInfoMsg.create({
          workflowId: "flow.py::parallel-skips",
          displayName: "Parallel skips",
          nodeIds: ["source", "middle", "first", "second"],
          graph: {
            source: { children: ["middle", "first", "second"] },
            middle: { children: ["first", "second"] },
            first: { children: [] },
            second: { children: [] },
          },
          nodeTypes: {
            source: "step",
            middle: "step",
            first: "step",
            second: "step",
          },
          displayNames: {
            source: "Source",
            middle: "Middle",
            first: "First",
            second: "Second",
          },
        })}
        onOpenNode={() => undefined}
      />,
    );

    expect(graphMetrics.edgeSets.at(-1)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: "source->first",
          targetHandle: "target-bottom",
          type: "skip",
          data: { lane: 0 },
        }),
        expect.objectContaining({
          id: "source->second",
          targetHandle: "target-bottom",
          type: "skip",
          data: { lane: 1 },
        }),
      ]),
    );
  });

  it("updates running seconds without relayout and keeps completed seconds stable", () => {
    vi.useFakeTimers();
    vi.setSystemTime(10_000);
    const topology = WorkflowTopologyMsg.create({
      nodeIds: ["agent"],
      graph: { agent: { children: [] } },
      nodeTypes: { agent: "step" },
      displayNames: { agent: "Timed agent" },
    });
    const view = render(
      <GraphCanvas
        runTopology={topology}
        runNodes={[
          NodeSnapshotMsg.create({
            nodeId: "agent",
            name: "Timed agent",
            nodeType: "step",
            status: "running",
            startedAt: 5,
          }),
        ]}
        onOpenNode={() => undefined}
      />,
    );

    expect(screen.getByText("5.0s")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect Timed agent" }).closest(".node-card"))
      .toHaveClass("status-running");
    act(() => vi.advanceTimersByTime(1_000));
    expect(screen.getByText("6.0s")).toBeInTheDocument();
    expect(graphMetrics.layoutCount).toBe(1);

    view.rerender(
      <GraphCanvas
        runTopology={topology}
        runNodes={[
          NodeSnapshotMsg.create({
            nodeId: "agent",
            name: "Timed agent",
            nodeType: "step",
            status: "success",
            startedAt: 5,
            endedAt: 7,
          }),
        ]}
        onOpenNode={() => undefined}
      />,
    );
    expect(screen.getByText("2.0s")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect Timed agent" }).closest(".node-card"))
      .toHaveClass("status-success");
    act(() => vi.advanceTimersByTime(5_000));
    expect(screen.getByText("2.0s")).toBeInTheDocument();
    expect(graphMetrics.layoutCount).toBe(1);
    view.unmount();
  });
});
