import { memo, type ReactNode, useEffect, useMemo, useState } from "react";
import {
  Background,
  BaseEdge,
  Controls,
  Handle,
  MarkerType,
  Panel,
  Position,
  ReactFlow,
  useStore,
  type Edge,
  type EdgeProps,
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
export interface SkillMetadata {
  name: string;
  instructions: string;
}

export interface ToolMetadata {
  name: string;
  description: string;
}

export interface AgentFieldSchemas {
  inputs: FieldMetadata[];
  outputs: FieldMetadata[];
}

export interface AgentDeclaration extends AgentFieldSchemas {
  instructions: string;
  model?: unknown;
  runtime?: unknown;
  skills: SkillMetadata[];
  tools: ToolMetadata[];
}

interface CardData extends Record<string, unknown> {
  label: string;
  nodeType: string;
  identity?: string;
  status?: string;
  error?: string;
  startedAt?: number;
  endedAt?: number;
  declaration?: AgentFieldSchemas;
  onOpen: () => void;
}

function skills(value: unknown): SkillMetadata[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) =>
    isUnknownRecord(item) && typeof item.name === "string"
      ? [
          {
            name: item.name,
            instructions: typeof item.instructions === "string" ? item.instructions : "",
          },
        ]
      : [],
  );
}

function tools(value: unknown): ToolMetadata[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) =>
    isUnknownRecord(item) && typeof item.name === "string"
      ? [
          {
            name: item.name,
            description: typeof item.description === "string" ? item.description : "",
          },
        ]
      : [],
  );
}

function fields(value: unknown): FieldMetadata[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isUnknownRecord(item) || typeof item.name !== "string") return [];
    return [
      {
        name: item.name,
        type:
          typeof item.type === "string"
            ? item.type
            : typeof item.annotation === "string"
              ? item.annotation
              : undefined,
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
      skills: skills(metadata.skills),
      tools: tools(metadata.tools),
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

const NodeDuration = memo(
  ({
    startedAt,
    endedAt,
    running,
  }: {
    startedAt: number;
    endedAt?: number;
    running: boolean;
  }) => {
    const [nowSeconds, setNowSeconds] = useState(() => Date.now() / 1000);
    useEffect(() => {
      if (!running || endedAt) return;
      setNowSeconds(Date.now() / 1000);
      const interval = window.setInterval(
        () => setNowSeconds(Date.now() / 1000),
        1_000,
      );
      return () => window.clearInterval(interval);
    }, [endedAt, running]);
    const end = endedAt || nowSeconds;
    return (
      <span className="node-duration">
        {Math.max(0, end - startedAt).toFixed(1)}s
      </span>
    );
  },
);
NodeDuration.displayName = "NodeDuration";

const WorkflowNodeCard = memo(({ data }: NodeProps<Node<CardData>>) => {
  return (
    <article
      className={`node-card ${data.status ? `status-${data.status}` : "blueprint"}`}
    >
      <Handle
        id="target-left"
        className="node-handle"
        type="target"
        position={Position.Left}
        isConnectable={false}
      />
      <Handle
        id="target-bottom"
        className="node-handle"
        type="target"
        position={Position.Bottom}
        isConnectable={false}
      />
      <button
        type="button"
        className="node-card-action"
        onClick={data.onOpen}
        aria-label={`Inspect ${data.label}${data.identity ? ` ${data.identity}` : ""}`}
      />
      <header className="node-header">
        <span className="node-kicker">{data.nodeType}</span>
        <strong className="node-title">{data.label}</strong>
        {data.identity && <span className="node-identity">{data.identity}</span>}
        {data.status && <span className="node-status">{data.status}</span>}
        {data.startedAt && (
          <NodeDuration
            startedAt={data.startedAt}
            endedAt={data.endedAt}
            running={data.status === "running"}
          />
        )}
      </header>
      {data.error && <span className="node-error">{data.error}</span>}
      {data.declaration && (
        <div className="field-grid node-declaration">
          <section className="node-fields node-inputs" aria-label="Inputs">
            <small>Inputs</small>
            {data.declaration.inputs.length ? (
              data.declaration.inputs.map((field) => (
                <span className="field node-field" key={`input-${field.name}`}>
                  <span className="node-field-name">{field.name}</span>
                  {field.type && <code>{field.type}</code>}
                </span>
              ))
            ) : (
              <span className="node-field-empty">None</span>
            )}
          </section>
          <section className="node-fields node-outputs" aria-label="Outputs">
            <small>Outputs</small>
            {data.declaration.outputs.length ? (
              data.declaration.outputs.map((field) => (
                <span className="field node-field" key={`output-${field.name}`}>
                  <span className="node-field-name">{field.name}</span>
                  {field.type && <code>{field.type}</code>}
                </span>
              ))
            ) : (
              <span className="node-field-empty">None</span>
            )}
          </section>
        </div>
      )}
      <Handle
        id="source-right"
        className="node-handle"
        type="source"
        position={Position.Right}
        isConnectable={false}
      />
      <Handle
        id="source-bottom"
        className="node-handle"
        type="source"
        position={Position.Bottom}
        isConnectable={false}
      />
    </article>
  );
});
WorkflowNodeCard.displayName = "WorkflowNodeCard";

interface SkipEdgeData extends Record<string, unknown> {
  lane: number;
}

type SkipEdge = Edge<SkipEdgeData, "skip">;

const SKIP_EDGE_CLEARANCE = 56;
const SKIP_EDGE_LANE_GAP = 32;

const SkipEdge = memo(
  ({
    data,
    markerEnd,
    sourceX,
    sourceY,
    style,
    targetX,
    targetY,
  }: EdgeProps<SkipEdge>) => {
    const graphBottom = useStore((state) =>
      Math.max(
        ...Array.from(
          state.nodeLookup.values(),
          (node) => node.internals.positionAbsolute.y + (node.measured.height ?? 0),
        ),
      ),
    );
    if (data === undefined) {
      throw new Error("Skip edge is missing its routing lane");
    }
    const routeY = graphBottom + SKIP_EDGE_CLEARANCE + data.lane * SKIP_EDGE_LANE_GAP;
    const path = `M ${sourceX},${sourceY} L ${sourceX},${routeY} L ${targetX},${routeY} L ${targetX},${targetY}`;

    return <BaseEdge path={path} markerEnd={markerEnd} style={style} />;
  },
);
SkipEdge.displayName = "SkipEdge";

const NODE_TYPES = { workflow: WorkflowNodeCard };
const EDGE_TYPES = { skip: SkipEdge };
const FIT_VIEW_OPTIONS = { padding: 0.24 };


function invocationIdentity(nodeId: string, label: string): string {
  const suffix = nodeId.startsWith(`${label}_`) ? nodeId.slice(label.length + 1) : "";
  return suffix && /^\d+$/.test(suffix) ? `#${suffix}` : nodeId;
}

type TopologyView = Pick<
  WorkflowTopologyMsg,
  "nodeIds" | "graph" | "nodeTypes" | "displayNames"
>;

interface GraphLayout {
  edges: Edge[];
  positions: Record<string, { x: number; y: number }>;
}

function createGraphLayout(topology: Pick<TopologyView, "nodeIds" | "graph">): GraphLayout {
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
  const positions = Object.fromEntries(
    Object.entries(rows).flatMap(([depth, nodeIds]) =>
      nodeIds.map((nodeId, row) => [
        nodeId,
        {
          x: Number(depth) * 500,
          y: row * 220 - ((nodeIds.length - 1) * 110),
        },
      ]),
    ),
  );
  const seen = new Set<string>();
  const edges: Edge[] = [];
  let skipEdgeLane = 0;
  for (const [source, children] of Object.entries(topology.graph)) {
    for (const target of children.children) {
      const id = `${source}->${target}`;
      if (seen.has(id)) continue;
      seen.add(id);
      const isSkipEdge = depths[target] !== (depths[source] ?? 0) + 1;
      edges.push({
        id,
        source,
        target,
        sourceHandle: isSkipEdge ? "source-bottom" : "source-right",
        targetHandle: isSkipEdge ? "target-bottom" : "target-left",
        markerEnd: { type: MarkerType.ArrowClosed },
        type: isSkipEdge ? "skip" : "step",
        data: isSkipEdge ? { lane: skipEdgeLane++ } : undefined,
        className: "dag-edge",
      });
    }
  }
  return { edges, positions };
}

interface GraphCanvasProps {
  workflow?: FlowInfoMsg;
  runTopology?: WorkflowTopologyMsg;
  runNodes?: NodeSnapshotMsg[];
  topLeftPanel?: ReactNode;
  bottomRightPanel?: ReactNode;
  onOpenNode: (nodeId: string) => void;
}

function GraphCanvasView({
  workflow,
  runTopology,
  runNodes = [],
  topLeftPanel,
  bottomRightPanel,
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
  const topologyNodeIds = topology?.nodeIds;
  const topologyGraph = topology?.graph;
  const layout = useMemo(
    () =>
      topologyNodeIds && topologyGraph
        ? createGraphLayout({ nodeIds: topologyNodeIds, graph: topologyGraph })
        : { edges: [], positions: {} },
    [topologyGraph, topologyNodeIds],
  );
  const openCallbacks = useMemo(
    () =>
      Object.fromEntries(
        (topologyNodeIds ?? []).map((nodeId) => [nodeId, () => onOpenNode(nodeId)]),
      ),
    [onOpenNode, topologyNodeIds],
  );
  const nodes = useMemo(() => {
    if (!topology) return [];
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
        position: layout.positions[nodeId],
        data: {
          label: labels[nodeId],
          identity:
            labelCounts[labels[nodeId]] > 1
              ? invocationIdentity(nodeId, labels[nodeId])
              : undefined,
          nodeType: topology.nodeTypes[nodeId] || runtimeNode?.nodeType || "step",
          status: runtimeNode?.status,
          error: runtimeNode?.error,
          startedAt: runtimeNode?.startedAt || undefined,
          endedAt: runtimeNode?.endedAt || undefined,
          declaration: runTopology
            ? parseAgentFieldSchemas(runTopology.agentFieldSchemasJson[nodeId])
            : parseAgentDeclaration(workflow?.agentMetadataJson[nodeId]),
          onOpen: openCallbacks[nodeId],
        },
      };
    });
    return nodes;
  }, [layout.positions, openCallbacks, runNodes, runTopology, topology, workflow]);

  return (
    <ReactFlow
      nodes={nodes}
      edges={layout.edges}
      nodeTypes={NODE_TYPES}
      edgeTypes={EDGE_TYPES}
      fitView
      fitViewOptions={FIT_VIEW_OPTIONS}
      minZoom={0.25}
      maxZoom={1.8}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable
      proOptions={{ hideAttribution: true }}
    >
      {topLeftPanel && (
        <Panel position="top-left" className="dag-panel dag-runs-panel nodrag nopan nowheel">
          {topLeftPanel}
        </Panel>
      )}
      {bottomRightPanel && (
        <Panel position="bottom-right" className="dag-panel dag-actions-panel nodrag nopan">
          {bottomRightPanel}
        </Panel>
      )}
      <Background color="rgba(255,255,255,.06)" gap={24} size={1} />
      <Controls showInteractive={false} />
    </ReactFlow>
  );
}

function sameRunNodeState(
  left: readonly NodeSnapshotMsg[] | undefined,
  right: readonly NodeSnapshotMsg[] | undefined,
) {
  if (left === right) return true;
  if ((left?.length ?? 0) !== (right?.length ?? 0)) return false;
  return (left ?? []).every((node, index) => {
    const other = right?.[index];
    return (
      other !== undefined &&
      node.nodeId === other.nodeId &&
      node.name === other.name &&
      node.nodeType === other.nodeType &&
      node.status === other.status &&
      node.error === other.error &&
      node.startedAt === other.startedAt &&
      node.endedAt === other.endedAt
    );
  });
}

export const GraphCanvas = memo(
  GraphCanvasView,
  (left, right) =>
    left.workflow === right.workflow &&
    left.runTopology === right.runTopology &&
    left.topLeftPanel === right.topLeftPanel &&
    left.bottomRightPanel === right.bottomRightPanel &&
    left.onOpenNode === right.onOpenNode &&
    sameRunNodeState(left.runNodes, right.runNodes),
);
