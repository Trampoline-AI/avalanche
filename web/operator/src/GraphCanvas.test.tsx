import type { ComponentType } from "react";

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

interface RenderedNode {
  id: string;
  selected?: boolean;
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
  zoom: 1,
  screenToFlowPosition: vi.fn(() => ({ x: 420, y: 240 })),
  setCenter: vi.fn(),
}));

vi.mock("@xyflow/react", () => {
  return {
    Background: () => null,
    BackgroundVariant: { Dots: "dots" },
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
    useViewport: () => ({ x: 0, y: 0, zoom: graphMetrics.zoom }),
    useReactFlow: () => ({
      screenToFlowPosition: graphMetrics.screenToFlowPosition,
      setCenter: graphMetrics.setCenter,
    }),
    ReactFlow: ({
      nodes,
      edges,
      nodeTypes,
    }: {
      nodes: RenderedNode[];
      edges: RenderedEdge[];
      nodeTypes: Record<string, ComponentType<{ data: unknown; selected?: boolean }>>;
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
            <NodeComponent key={node.id} data={node.data} selected={node.selected} />
          ))}
        </div>
      );
    },
  };
});

import { GraphCanvas, parseAgentDeclaration } from "./GraphCanvas";
import { Markdown } from "./Markdown";
import { FlowInfoMsg, NodeSnapshotMsg, TraceDescriptorMsg, WorkflowTopologyMsg } from "./model";

function metadata(field: string) {
  return JSON.stringify({
    signature: {
      instructions: "Current instruction.\nSecond instruction line.",
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
  agentNodeIds: ["agent"],
});

describe("GraphCanvas", () => {
  beforeEach(() => {
    graphMetrics.layoutCount = 0;
    graphMetrics.renderCount = 0;
    graphMetrics.seenPositions = new WeakSet<object>();
    graphMetrics.nodeSets = [];
    graphMetrics.edgeSets = [];
    graphMetrics.zoom = 1;
    graphMetrics.screenToFlowPosition.mockClear();
    graphMetrics.setCenter.mockClear();
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
      <Markdown className="bounded-markdown" sourceCharacterBudget={sourceCharacterBudget}>
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
      <Markdown className="bounded-markdown" sourceCharacterBudget={sourceCharacterBudget}>
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
      <GraphCanvas workflow={workflow} selectedNodeId="agent" onOpenNode={() => undefined} />,
    );

    expect(screen.getByText("Current agent")).toBeInTheDocument();
    expect(screen.getByText("Store")).toBeInTheDocument();
    expect(screen.getByText("current_input")).toBeInTheDocument();
    expect(screen.getByText("Current instruction.")).toHaveClass(
      "node-instruction-line",
      "self-stretch",
      "overflow-hidden",
      "line-clamp-2",
    );
    expect(screen.getByText("Current instruction.")).not.toHaveClass("whitespace-nowrap");
    expect(screen.getByTestId("edge-count")).toHaveTextContent("1");
    const currentAgentCard = screen
      .getByRole("button", { name: "Inspect Current agent" })
      .closest("article");
    expect(currentAgentCard).toHaveAttribute("data-node-kind", "agent");
    expect(currentAgentCard).toHaveClass("border-acid!");
    expect(
      screen.getByRole("button", { name: "Inspect Store" }).closest("article"),
    ).toHaveAttribute("data-node-kind", "standard");
    expect(
      screen.getByRole("button", { name: "Inspect Store" }).closest("article"),
    ).not.toHaveClass("border-acid!");

    view.rerender(
      <GraphCanvas
        workflow={workflow}
        runTopology={WorkflowTopologyMsg.create({
          nodeIds: ["agent"],
          graph: { agent: { children: [] } },
          nodeTypes: { agent: "step" },
          displayNames: { agent: "Recorded agent" },
          agentFieldSchemasJson: { agent: fieldSchemas("recorded_input") },
          agentInstructionLines: { agent: "Recorded instruction." },
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
    const historicalAgentCard = screen
      .getByRole("button", { name: "Inspect Recorded agent" })
      .closest("article");
    expect(historicalAgentCard).toHaveAttribute("data-node-kind", "agent");
    expect(historicalAgentCard?.querySelector(".node-kicker")).toHaveTextContent("agent");

    expect(screen.getByText("Recorded agent")).toBeInTheDocument();
    expect(screen.getByText("recorded_input")).toBeInTheDocument();
    expect(screen.getByText("Recorded instruction.")).toBeInTheDocument();
    expect(screen.queryByText("recorded failure")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Inspect Recorded agent" }).closest(".node-card"),
    ).toHaveClass("status-failed");
    expect(historicalAgentCard).toHaveClass("before:bg-agent", "border-line");
    expect(historicalAgentCard?.querySelector(".node-title")).toHaveClass("text-ink");
    expect(historicalAgentCard?.querySelector(".node-status-icon")).toHaveClass(
      "lucide-x",
      "size-3",
      "text-failed",
    );
    expect(screen.getByText("failed")).toHaveClass("text-failed");
    expect(screen.queryByText("Store")).not.toBeInTheDocument();
    expect(screen.queryByText("current_input")).not.toBeInTheDocument();
  });

  it("renders source, step, and destination docstring summaries in current and recorded DAGs", () => {
    const view = render(
      <GraphCanvas
        workflow={FlowInfoMsg.create({
          workflowId: "flow.py::standard",
          displayName: "Standard",
          nodeIds: ["load", "normalize", "store"],
          graph: {
            load: { children: ["normalize"] },
            normalize: { children: ["store"] },
            store: { children: [] },
          },
          nodeTypes: { load: "source", normalize: "step", store: "dest" },
          displayNames: { load: "Load", normalize: "Normalize", store: "Store" },
          standardStepDocstringLines: {
            load: "Load incoming records.",
            normalize: "Normalize incoming records.",
            store: "Store normalized records.",
          },
        })}
        onOpenNode={() => undefined}
      />,
    );

    for (const summary of [
      "Load incoming records.",
      "Normalize incoming records.",
      "Store normalized records.",
    ]) {
      expect(screen.getByText(summary)).toHaveClass("node-instruction-line");
    }

    view.rerender(
      <GraphCanvas
        runTopology={WorkflowTopologyMsg.create({
          nodeIds: ["load", "normalize", "store"],
          graph: {
            load: { children: ["normalize"] },
            normalize: { children: ["store"] },
            store: { children: [] },
          },
          nodeTypes: { load: "source", normalize: "step", store: "dest" },
          displayNames: { load: "Load", normalize: "Normalize", store: "Store" },
          standardStepDocstringLines: {
            load: "Load the recorded input.",
            normalize: "Normalize the recorded input.",
            store: "Store the recorded output.",
          },
        })}
        runNodes={[
          NodeSnapshotMsg.create({
            nodeId: "load",
            name: "Load",
            nodeType: "source",
            status: "success",
          }),
          NodeSnapshotMsg.create({
            nodeId: "normalize",
            name: "Normalize",
            nodeType: "step",
            status: "success",
          }),
          NodeSnapshotMsg.create({
            nodeId: "store",
            name: "Store",
            nodeType: "dest",
            status: "success",
          }),
        ]}
        onOpenNode={() => undefined}
      />,
    );

    for (const summary of [
      "Load the recorded input.",
      "Normalize the recorded input.",
      "Store the recorded output.",
    ]) {
      expect(screen.getByText(summary)).toHaveClass("node-instruction-line");
    }
    expect(screen.queryByText("Load incoming records.")).not.toBeInTheDocument();
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
    expect(screen.getByText("repeat #1", { selector: ".node-title" })).toBeInTheDocument();
    expect(screen.getByText("repeat #2", { selector: ".node-title" })).toBeInTheDocument();
  });

  it("reuses nodes and layout for detail-only rerenders while applying status and topology changes", () => {
    const onOpenNode = vi.fn();
    const topology = WorkflowTopologyMsg.create({
      nodeIds: ["agent"],
      graph: { agent: { children: [] } },
      nodeTypes: { agent: "step" },
      displayNames: { agent: "Recorded agent" },
      agentFieldSchemasJson: { agent: fieldSchemas("input") },
    });
    const runningNode = NodeSnapshotMsg.create({
      nodeId: "agent",
      name: "Recorded agent",
      nodeType: "step",
      status: "running",
      startedAt: 1,
    });
    const view = render(
      <GraphCanvas runTopology={topology} runNodes={[runningNode]} onOpenNode={onOpenNode} />,
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
    const successCard = screen
      .getByRole("button", { name: "Inspect Recorded agent" })
      .closest("article");
    expect(successCard).toHaveClass("before:bg-agent", "border-line");
    expect(successCard?.querySelector(".node-kicker")).toHaveClass("text-agent");
    expect(successCard?.querySelector(".node-title")).toHaveClass("text-ink");
    expect(successCard?.querySelector(".node-status-icon")).toHaveClass(
      "lucide-check",
      "size-3",
      "text-success",
    );
    expect(screen.getByText("success")).toHaveClass("text-success");
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
    const requestAnimationFrame = vi
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation((callback) => {
        callback(0);
        return 0;
      });
    fireEvent.click(screen.getByRole("button", { name: "Inspect Renamed agent" }));
    expect(onOpenNode).toHaveBeenCalledWith("agent");
    expect(graphMetrics.screenToFlowPosition).toHaveBeenCalledOnce();
    expect(graphMetrics.setCenter).toHaveBeenCalledWith(420, 240, {
      zoom: 1.2,
      duration: 200,
    });
    requestAnimationFrame.mockRestore();
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
    expect(parsed?.skills).toEqual([{ name: "audit", instructions: "Check **every** field." }]);
    expect(parsed?.tools).toEqual([{ name: "lookup", description: "Look up a record." }]);

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

  it("uses fixed title sizes across the detail zoom threshold", () => {
    graphMetrics.zoom = 0.99;
    const view = render(<GraphCanvas workflow={workflow} onOpenNode={() => undefined} />);

    const compactCard = screen
      .getByRole("button", { name: "Inspect Current agent" })
      .closest(".node-card");
    expect(compactCard).toHaveClass("node-card--compact");
    expect(compactCard).toHaveClass("justify-center", "gap-0");
    expect(compactCard?.querySelector(".node-card-details")).toBeInTheDocument();
    expect(compactCard?.querySelector(".node-kicker")).toHaveClass("node-card-meta");
    expect(compactCard?.querySelector(".node-title")).toHaveClass("text-xl");

    graphMetrics.zoom = 1;
    view.rerender(<GraphCanvas workflow={workflow} onOpenNode={() => undefined} />);
    const expandedCard = screen
      .getByRole("button", { name: "Inspect Current agent" })
      .closest(".node-card");
    expect(expandedCard).not.toHaveClass("node-card--compact");
    expect(expandedCard?.querySelector(".node-title")).toHaveClass("text-sm");
  });

  it("keeps failed error messages out of DAG cards", () => {
    graphMetrics.zoom = 0.99;
    const topology = WorkflowTopologyMsg.create({
      nodeIds: ["failed_step_1"],
      graph: { failed_step_1: { children: [] } },
      nodeTypes: { failed_step_1: "step" },
      displayNames: { failed_step_1: "Failed step" },
    });
    render(
      <GraphCanvas
        runTopology={topology}
        runNodes={[
          NodeSnapshotMsg.create({
            nodeId: "failed_step_1",
            name: "Failed step",
            nodeType: "step",
            status: "failed",
            error: "The retained log is the error detail surface.",
          }),
        ]}
        onOpenNode={() => undefined}
      />,
    );

    const card = screen
      .getByRole("button", { name: "Inspect Failed step" })
      .closest(".node-card");
    expect(card).toHaveClass("status-failed", "node-card--compact");
    expect(card).not.toHaveTextContent("The retained log is the error detail surface.");
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
    graphMetrics.zoom = 0.99;
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
            runningElapsedSeconds: 4.5,
          }),
        ]}
        onOpenNode={() => undefined}
      />,
    );

    expect(screen.getByText("4.5s")).toBeInTheDocument();
    const compactDuration = screen.getByText("4.5s");
    expect(compactDuration.parentElement).toHaveClass("node-card");
    expect(compactDuration).toHaveClass(
      "top-3",
      "right-3",
      "text-sm",
      "transition-[font-size]",
      "duration-150",
    );
    expect(
      screen.getByRole("button", { name: "Inspect Timed agent" }).closest(".node-card"),
    ).toHaveClass("status-running", "gradient-animate");
    act(() => vi.advanceTimersByTime(100));
    expect(screen.getByText("4.6s")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(900));
    expect(screen.getByText("5.5s")).toBeInTheDocument();
    expect(graphMetrics.layoutCount).toBe(1);
    view.unmount();
    act(() => vi.advanceTimersByTime(7_000));
    const reentered = render(
      <GraphCanvas
        runTopology={topology}
        runNodes={[
          NodeSnapshotMsg.create({
            nodeId: "agent",
            name: "Timed agent",
            nodeType: "step",
            status: "running",
            startedAt: 5,
            runningElapsedSeconds: 12.5,
          }),
        ]}
        onOpenNode={() => undefined}
      />,
    );
    expect(screen.getByText("12.5s")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(100));
    expect(screen.getByText("12.6s")).toBeInTheDocument();

    graphMetrics.zoom = 1;
    reentered.rerender(
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
    expect(screen.getByText("2.0s")).toHaveClass("top-4", "right-4", "text-[9px]");
    expect(
      screen.getByRole("button", { name: "Inspect Timed agent" }).closest(".node-card"),
    ).toHaveClass("status-success");
    act(() => vi.advanceTimersByTime(5_000));
    expect(screen.getByText("2.0s")).toBeInTheDocument();
    expect(graphMetrics.layoutCount).toBe(2);
    reentered.unmount();
  });
});
