import type { ComponentType } from "react";

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

interface RenderedNode {
  id: string;
  position: object;
  data: {
    onOpen: () => void;
  };
}

const graphMetrics = vi.hoisted(() => ({
  layoutCount: 0,
  renderCount: 0,
  seenPositions: new WeakSet<object>(),
  nodeSets: [] as RenderedNode[][],
}));

vi.mock("@xyflow/react", () => {
  return {
    Background: () => null,
    Controls: () => null,
    Handle: () => null,
    MarkerType: { ArrowClosed: "arrow-closed" },
    Position: { Left: "left", Right: "right" },
    ReactFlow: ({
      nodes,
      edges,
      nodeTypes,
    }: {
      nodes: RenderedNode[];
      edges: Array<{ id: string }>;
      nodeTypes: Record<string, ComponentType<{ data: unknown }>>;
    }) => {
      graphMetrics.renderCount += 1;
      graphMetrics.nodeSets.push(nodes);
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

import { GraphCanvas } from "./GraphCanvas";
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
});
