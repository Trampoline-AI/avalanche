import type { ComponentType } from "react";

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

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
      nodes: Array<{ id: string; data: unknown }>;
      edges: Array<{ id: string }>;
      nodeTypes: Record<string, ComponentType<{ data: unknown }>>;
    }) => {
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
});
