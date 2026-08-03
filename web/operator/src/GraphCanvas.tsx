import { memo, useMemo } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";

import type {
  FlowInfoMsg,
  NodeSnapshotMsg,
  WorkflowTopologyMsg,
} from "./generated/operator";
import { isUnknownRecord } from "./guards";

interface FieldMetadata {
  name: string;
  type?: string;
  description?: string;
}

export interface AgentFieldSchemas {
  inputs: FieldMetadata[];
  outputs: FieldMetadata[];
}

export interface AgentDeclaration extends AgentFieldSchemas {
  instructions: string;
  model?: unknown;
  runtime?: unknown;
  skills?: unknown;
  tools?: unknown;
}

interface CardData extends Record<string, unknown> {
  label: string;
  nodeType: string;
  identity?: string;
  status?: string;
  error?: string;
  duration?: string;
  declaration?: AgentFieldSchemas;
  onOpen: () => void;
}


function fields(value: unknown): FieldMetadata[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isUnknownRecord(item) || typeof item.name !== "string") return [];
    return [
      {
        name: item.name,
        type: typeof item.type === "string" ? item.type : undefined,
        description:
          typeof item.description === "string" ? item.description : undefined,
      },
    ];
  });
}

export function parseAgentDeclaration(raw: string | undefined): AgentDeclaration | undefined {
  if (!raw) return undefined;
  try {
    const metadata: unknown = JSON.parse(raw);
    if (!isUnknownRecord(metadata)) return undefined;
    const signature = isUnknownRecord(metadata.signature) ? metadata.signature : {};
    return {
      instructions:
        typeof signature.instructions === "string" ? signature.instructions : "",
      inputs: fields(signature.inputs),
      outputs: fields(signature.outputs),
      model: metadata.models,
      runtime: metadata.runtime,
      skills: metadata.skills,
      tools: metadata.tools,
    };
  } catch {
    return undefined;
  }
}

export function parseAgentFieldSchemas(raw: string | undefined): AgentFieldSchemas | undefined {
  if (!raw) return undefined;
  try {
    const metadata: unknown = JSON.parse(raw);
    if (!isUnknownRecord(metadata)) return undefined;
    return {
      inputs: fields(metadata.inputs),
      outputs: fields(metadata.outputs),
    };
  } catch {
    return undefined;
  }
}

const WorkflowNodeCard = memo(({ data }: NodeProps<Node<CardData>>) => (
  <button
    type="button"
    className={`node-card ${data.status ? `status-${data.status}` : "blueprint"}`}
    onClick={data.onOpen}
    aria-label={`Inspect ${data.label}${data.identity ? ` ${data.identity}` : ""}`}
  >
    <Handle type="target" position={Position.Left} isConnectable={false} />
    <span className="node-kicker">{data.nodeType}</span>
    <strong>{data.label}</strong>
    {data.identity && <span className="node-identity">{data.identity}</span>}
    {data.status && <span className="node-status">{data.status}</span>}
    {data.duration && <span className="node-duration">{data.duration}</span>}
    {data.error && <span className="node-error">{data.error}</span>}
    {data.declaration && (
      <span className="field-grid">
        <span>
          <small>Inputs</small>
          {data.declaration.inputs.map((field) => (
            <span className="field" key={`input-${field.name}`}>
              {field.name}
            </span>
          ))}
        </span>
        <span>
          <small>Outputs</small>
          {data.declaration.outputs.map((field) => (
            <span className="field" key={`output-${field.name}`}>
              {field.name}
            </span>
          ))}
        </span>
      </span>
    )}
    <Handle type="source" position={Position.Right} isConnectable={false} />
  </button>
));
WorkflowNodeCard.displayName = "WorkflowNodeCard";

function positions(topology: TopologyView): Record<string, { x: number; y: number }> {
  const incoming = Object.fromEntries(topology.nodeIds.map((nodeId) => [nodeId, 0]));
  for (const edges of Object.values(topology.graph)) {
    for (const child of edges.children) incoming[child] = (incoming[child] ?? 0) + 1;
  }
  const depths: Record<string, number> = {};
  const queue = topology.nodeIds.filter((nodeId) => incoming[nodeId] === 0);
  for (const nodeId of queue) depths[nodeId] = 0;
  for (let index = 0; index < queue.length; index += 1) {
    const parent = queue[index];
    for (const child of topology.graph[parent]?.children ?? []) {
      depths[child] = Math.max(depths[child] ?? 0, depths[parent] + 1);
      incoming[child] -= 1;
      if (incoming[child] === 0) queue.push(child);
    }
  }
  const rows: Record<number, string[]> = {};
  for (const nodeId of topology.nodeIds) {
    const depth = depths[nodeId] ?? 0;
    (rows[depth] ??= []).push(nodeId);
  }
  return Object.fromEntries(
    Object.entries(rows).flatMap(([depth, nodeIds]) =>
      nodeIds.map((nodeId, row) => [
        nodeId,
        { x: Number(depth) * 330, y: row * 220 - ((nodeIds.length - 1) * 110) },
      ]),
    ),
  );
}

function elapsed(node: NodeSnapshotMsg): string | undefined {
  if (!node.startedAt) return undefined;
  const end = node.endedAt || Date.now() / 1000;
  const seconds = Math.max(0, end - node.startedAt);
  return seconds < 60 ? `${seconds.toFixed(1)}s` : `${Math.floor(seconds / 60)}m`;
}

function invocationIdentity(nodeId: string, label: string): string {
  const suffix = nodeId.startsWith(`${label}_`) ? nodeId.slice(label.length + 1) : "";
  return suffix && /^\d+$/.test(suffix) ? `#${suffix}` : nodeId;
}

type TopologyView = Pick<
  WorkflowTopologyMsg,
  "nodeIds" | "graph" | "nodeTypes" | "displayNames"
>;

interface GraphCanvasProps {
  workflow?: FlowInfoMsg;
  runTopology?: WorkflowTopologyMsg;
  runNodes?: NodeSnapshotMsg[];
  onOpenNode: (nodeId: string) => void;
}

export function GraphCanvas({
  workflow,
  runTopology,
  runNodes = [],
  onOpenNode,
}: GraphCanvasProps) {
  const topology = useMemo<TopologyView | undefined>(() => {
    if (runTopology) return runTopology;
    if (!workflow) return undefined;
    return {
      nodeIds: workflow.nodeIds,
      graph: workflow.graph,
      nodeTypes: workflow.nodeTypes,
      displayNames: workflow.displayNames,
    };
  }, [runTopology, workflow]);
  const graph = useMemo(() => {
    if (!topology) return { nodes: [], edges: [] };
    const layout = positions(topology);
    const runtimeNodes = Object.fromEntries(runNodes.map((node) => [node.nodeId, node]));
    const labels = Object.fromEntries(
      topology.nodeIds.map((nodeId) => [
        nodeId,
        topology.displayNames[nodeId] || runtimeNodes[nodeId]?.name || nodeId,
      ]),
    );
    const labelCounts = Object.values(labels).reduce<Record<string, number>>(
      (counts, label) => ({ ...counts, [label]: (counts[label] ?? 0) + 1 }),
      {},
    );
    const nodes: Node<CardData>[] = topology.nodeIds.map((nodeId) => {
      const runtimeNode = runtimeNodes[nodeId];
      return {
        id: nodeId,
        type: "workflow",
        position: layout[nodeId],
        data: {
          label: labels[nodeId],
          identity:
            labelCounts[labels[nodeId]] > 1
              ? invocationIdentity(nodeId, labels[nodeId])
              : undefined,
          nodeType: topology.nodeTypes[nodeId] || runtimeNode?.nodeType || "step",
          status: runtimeNode?.status,
          error: runtimeNode?.error,
          duration: runtimeNode ? elapsed(runtimeNode) : undefined,
          declaration: runTopology
            ? parseAgentFieldSchemas(runTopology.agentFieldSchemasJson[nodeId])
            : parseAgentDeclaration(workflow?.agentMetadataJson[nodeId]),
          onOpen: () => onOpenNode(nodeId),
        },
      };
    });
    const seen = new Set<string>();
    const edges: Edge[] = [];
    for (const [source, children] of Object.entries(topology.graph)) {
      for (const target of children.children) {
        const id = `${source}->${target}`;
        if (seen.has(id)) continue;
        seen.add(id);
        edges.push({
          id,
          source,
          target,
          markerEnd: { type: MarkerType.ArrowClosed },
          className: "dag-edge",
        });
      }
    }
    return { nodes, edges };
  }, [onOpenNode, runNodes, runTopology, topology, workflow]);

  return (
    <ReactFlow
      nodes={graph.nodes}
      edges={graph.edges}
      nodeTypes={{ workflow: WorkflowNodeCard }}
      fitView
      fitViewOptions={{ padding: 0.24 }}
      minZoom={0.25}
      maxZoom={1.8}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable
      proOptions={{ hideAttribution: true }}
    >
      <Background color="rgba(255,255,255,.06)" gap={24} size={1} />
      <Controls showInteractive={false} />
    </ReactFlow>
  );
}
