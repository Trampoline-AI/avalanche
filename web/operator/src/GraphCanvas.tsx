import {
  memo,
  type MouseEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Check, X } from "lucide-react";

import {
  Background,
  BackgroundVariant,
  BaseEdge,
  Controls,
  Handle,
  MarkerType,
  Panel,
  Position,
  ReactFlow,
  useReactFlow,
  useStore,
  useViewport,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from "@xyflow/react";

import type { FlowInfoMsg, NodeSnapshotMsg, WorkflowTopologyMsg } from "./generated/operator";
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
  isAgent: boolean;
  identity?: string;
  status?: string;
  error?: string;
  startedAt?: number;
  endedAt?: number;
  runningElapsedSeconds?: number;
  declaration?: AgentFieldSchemas;
  instructionLine?: string;
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
        description: typeof item.description === "string" ? item.description : undefined,
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
      instructions: typeof signature.instructions === "string" ? signature.instructions : "",
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
function firstInstructionLine(instructions: string | undefined): string | undefined {
  const [firstLine] = instructions?.split(/\r?\n/, 1) ?? [];
  const instructionLine = firstLine?.trim();
  return instructionLine || undefined;
}

const NODE_DETAIL_ZOOM_THRESHOLD = 1.0;
const FOCUSED_NODE_ZOOM = 1.2;

const NodeDuration = memo(
  ({
    startedAt,
    endedAt,
    runningElapsedSeconds,
    running,
    compact,
  }: {
    startedAt: number;
    endedAt?: number;
    running: boolean;
    runningElapsedSeconds: number;
    compact: boolean;
  }) => {
    const [nowMs, setNowMs] = useState(() => performance.now());
    const runningClock = useRef<
      { startedAt: number; elapsedSeconds: number; receivedAtMs: number } | undefined
    >(undefined);
    const isRunning = running && endedAt === undefined;
    if (
      isRunning &&
      (runningClock.current?.startedAt !== startedAt ||
        runningClock.current.elapsedSeconds !== runningElapsedSeconds)
    ) {
      runningClock.current = {
        startedAt,
        elapsedSeconds: runningElapsedSeconds,
        receivedAtMs: performance.now(),
      };
    }
    useEffect(() => {
      if (!isRunning) return;
      setNowMs(performance.now());
      const interval = window.setInterval(() => setNowMs(performance.now()), 100);
      return () => window.clearInterval(interval);
    }, [isRunning, runningElapsedSeconds, startedAt]);
    const elapsedSeconds =
      endedAt !== undefined
        ? Math.max(0, endedAt - startedAt)
        : isRunning
          ? Math.max(
              0,
              (runningClock.current?.elapsedSeconds ?? runningElapsedSeconds) +
                (nowMs - (runningClock.current?.receivedAtMs ?? nowMs)) / 1_000,
            )
          : 0;
    return (
      <span
        className={`node-duration absolute font-mono text-muted transition-[font-size] duration-150 ease-out motion-reduce:transition-none ${compact ? "top-3 right-3 text-sm" : "top-4 right-4 text-[9px]"}`}
      >
        {elapsedSeconds.toFixed(1)}s
      </span>
    );
  },
);
NodeDuration.displayName = "NodeDuration";

const WorkflowNodeCard = memo(({ data, selected }: NodeProps<Node<CardData>>) => {
  const { screenToFlowPosition, setCenter } = useReactFlow();
  const { zoom } = useViewport();
  const isCompact = zoom < NODE_DETAIL_ZOOM_THRESHOLD;
  const openAndFocusNode = useCallback(
    (event: MouseEvent<HTMLButtonElement>) => {
      const bounds = event.currentTarget.getBoundingClientRect();
      const nodeCenter = screenToFlowPosition({
        x: bounds.left + bounds.width / 2,
        y: bounds.top + bounds.height / 2,
      });
      data.onOpen();
      window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => {
          void setCenter(nodeCenter.x, nodeCenter.y, {
            zoom: FOCUSED_NODE_ZOOM,
            duration: 200,
          });
        });
      });
    },
    [data.onOpen, screenToFlowPosition, setCenter],
  );
  const agentClass = data.isAgent
    ? "node-agent before:pointer-events-none before:absolute before:inset-y-3 before:left-0 before:w-[3px] before:rounded-r-full before:bg-agent before:content-['']"
    : "";
  const statusColorClass =
    data.status === "success"
      ? "text-success"
      : data.status === "failed"
        ? "text-failed"
        : "text-muted";
  const statusClass =
    data.status === "success"
      ? "status-success"
      : data.status === "failed"
        ? "status-failed"
        : data.status === "running"
          ? "status-running gradient-animate border-[3px]"
          : "blueprint";
  return (
    <article
      className={`node-card ${isCompact ? "node-card--compact min-h-[100px] justify-center gap-0 px-4 py-3" : "min-h-[130px] gap-2 p-4"} relative flex w-[360px] cursor-pointer flex-col items-stretch rounded-xl border border-line bg-panel text-left shadow-[0_8px_24px_rgba(25,39,32,.08)] transition-[border-color,box-shadow,transform] duration-150 ease-out hover:-translate-y-px hover:border-acid hover:shadow-[0_10px_28px_rgba(25,39,32,.12)] motion-reduce:transition-none ${selected && data.status !== "running" ? "border-acid!" : ""} ${agentClass} ${statusClass}`}
      data-node-kind={data.isAgent ? "agent" : "standard"}
    >
      <Handle
        id="target-left"
        className="node-handle pointer-events-none size-px! min-h-0! min-w-0! border-0! bg-transparent! opacity-0"
        type="target"
        position={Position.Left}
        isConnectable={false}
      />
      <Handle
        id="target-bottom"
        className="node-handle pointer-events-none size-px! min-h-0! min-w-0! border-0! bg-transparent! opacity-0"
        type="target"
        position={Position.Bottom}
        isConnectable={false}
      />
      <button
        type="button"
        className="node-card-action absolute inset-0 z-[2] cursor-pointer rounded-[inherit] border-0 bg-transparent p-0 focus-visible:outline-3 focus-visible:outline-offset-3 focus-visible:outline-acid"
        onClick={openAndFocusNode}
        aria-label={`Inspect ${data.label}${data.identity ? ` ${data.identity}` : ""}`}
      />
      <header
        className={`node-header relative flex flex-col ${
          isCompact
            ? "min-h-0 items-center justify-center gap-0 pr-0 text-center"
            : "min-h-10 items-start gap-1"
        }`}
      >
        <span
          className={`node-card-meta node-kicker font-mono text-[8px] tracking-[.12em] uppercase ${data.isAgent ? "text-agent" : "text-secondary"}`}
        >
          {data.isAgent ? "agent" : data.nodeType}
        </span>
        <strong
          className={`node-title block self-stretch ${isCompact ? "text-xl" : "pr-[76px] text-sm"} leading-tight text-ink`}
        >
          {data.label}
          {data.status === "success" && (
            <Check
              aria-hidden="true"
              className={`node-status-icon ml-1 inline-block text-success transition-[width,height] duration-200 ease-out motion-reduce:transition-none ${isCompact ? "size-6 align-[-0.15em]" : "size-3 align-[-0.08em]"}`}
              strokeWidth={2.5}
            />
          )}
          {data.status === "failed" && (
            <X
              aria-hidden="true"
              className={`node-status-icon ml-1 inline-block text-failed transition-[width,height] duration-200 ease-out motion-reduce:transition-none ${isCompact ? "size-6 align-[-0.15em]" : "size-3 align-[-0.08em]"}`}
              strokeWidth={2.5}
            />
          )}
        </strong>
        {data.instructionLine && (
          <span
            className="node-card-meta node-instruction-line min-w-0 self-stretch overflow-hidden text-ellipsis line-clamp-2 font-mono text-[9px] leading-[1.35] text-secondary"
            title={data.instructionLine}
          >
            {data.instructionLine}
          </span>
        )}
        {data.identity && (
          <span className="node-card-meta node-identity font-mono text-[9px] text-secondary">
            {data.identity}
          </span>
        )}
        {data.status && (
          <span
            className={`node-card-meta node-status absolute top-[19px] right-0 font-mono text-[8px] uppercase ${statusColorClass}`}
          >
            {data.status}
          </span>
        )}
      </header>
      {data.startedAt && (
        <NodeDuration
          startedAt={data.startedAt}
          endedAt={data.endedAt}
          runningElapsedSeconds={data.runningElapsedSeconds ?? 0}
          running={data.status === "running"}
          compact={isCompact}
        />
      )}
      {data.declaration && (
        <div
          className={`node-card-details field-grid grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] overflow-hidden ${
            isCompact
              ? "min-h-0! max-h-0! gap-0! border-t-transparent! pt-0! opacity-0 pointer-events-none"
              : "min-h-[58px] gap-6 border-t border-line pt-2.5"
          }`}
        >
          <section
            className="node-fields node-inputs min-w-0 overflow-hidden [&>small]:mb-1.5 [&>small]:block [&>small]:font-mono [&>small]:text-[7px] [&>small]:tracking-[.08em] [&>small]:text-muted [&>small]:uppercase"
            aria-label="Inputs"
          >
            <small>Inputs</small>
            {data.declaration.inputs.length ? (
              data.declaration.inputs.map((field) => (
                <span
                  className="field node-field mt-[3px] flex min-h-3.5 min-w-0 items-start justify-start gap-1 font-mono text-[7px]/[1.35] text-[#36423c] [&>code]:min-w-0 [&>code]:[overflow-wrap:anywhere] [&>code]:text-[7px] [&>code]:text-muted"
                  key={`input-${field.name}`}
                >
                  <span className="node-field-name min-w-0 [overflow-wrap:anywhere]">
                    {field.name}
                  </span>
                  {field.type && <code>{field.type}</code>}
                </span>
              ))
            ) : (
              <span className="node-field-empty font-mono text-[7px] text-muted">None</span>
            )}
          </section>
          <section
            className="node-fields node-outputs min-w-0 overflow-hidden text-right [&>small]:mb-1.5 [&>small]:block [&>small]:font-mono [&>small]:text-[7px] [&>small]:tracking-[.08em] [&>small]:text-muted [&>small]:uppercase"
            aria-label="Outputs"
          >
            <small>Outputs</small>
            {data.declaration.outputs.length ? (
              data.declaration.outputs.map((field) => (
                <span
                  className="field node-field mt-[3px] flex min-h-3.5 min-w-0 items-start justify-end gap-1 font-mono text-[7px]/[1.35] text-[#36423c] [&>code]:min-w-0 [&>code]:[overflow-wrap:anywhere] [&>code]:text-[7px] [&>code]:text-muted"
                  key={`output-${field.name}`}
                >
                  <span className="node-field-name min-w-0 [overflow-wrap:anywhere]">
                    {field.name}
                  </span>
                  {field.type && <code>{field.type}</code>}
                </span>
              ))
            ) : (
              <span className="node-field-empty font-mono text-[7px] text-muted">None</span>
            )}
          </section>
        </div>
      )}
      <Handle
        id="source-right"
        className="node-handle pointer-events-none size-px! min-h-0! min-w-0! border-0! bg-transparent! opacity-0"
        type="source"
        position={Position.Right}
        isConnectable={false}
      />
      <Handle
        id="source-bottom"
        className="node-handle pointer-events-none size-px! min-h-0! min-w-0! border-0! bg-transparent! opacity-0"
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
  ({ data, markerEnd, sourceX, sourceY, style, targetX, targetY }: EdgeProps<SkipEdge>) => {
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
  "nodeIds" | "graph" | "nodeTypes" | "displayNames" | "agentInstructionLines"
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
          y: row * 220 - (nodeIds.length - 1) * 110,
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
  selectedNodeId?: string;
  onClearNode?: () => void;
  onOpenNode: (nodeId: string) => void;
}

function GraphCanvasView({
  workflow,
  runTopology,
  runNodes = [],
  topLeftPanel,
  bottomRightPanel,
  selectedNodeId,
  onClearNode,
  onOpenNode,
}: GraphCanvasProps) {
  const isCurrentWorkflow = workflow !== undefined && runTopology === undefined;
  const topology = useMemo<TopologyView | undefined>(() => {
    if (runTopology) return runTopology;
    if (!workflow) return undefined;
    return {
      nodeIds: workflow.nodeIds,
      graph: workflow.graph,
      nodeTypes: workflow.nodeTypes,
      displayNames: workflow.displayNames,
      agentInstructionLines: {},
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
  const agentNodeIds = useMemo(
    () =>
      new Set(
        runTopology
          ? Object.keys(runTopology.agentFieldSchemasJson)
          : (workflow?.agentNodeIds ?? []),
      ),
    [runTopology, workflow],
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
      const agentDeclaration = runTopology
        ? undefined
        : parseAgentDeclaration(workflow?.agentMetadataJson[nodeId]);
      const declaration = runTopology
        ? parseAgentFieldSchemas(runTopology.agentFieldSchemasJson[nodeId])
        : agentDeclaration;
      const instructionLine = runTopology
        ? runTopology.agentInstructionLines[nodeId] || undefined
        : firstInstructionLine(agentDeclaration?.instructions);
      return {
        id: nodeId,
        selected: nodeId === selectedNodeId,
        type: "workflow",
        position: layout.positions[nodeId],
        data: {
          label: labels[nodeId],
          identity:
            labelCounts[labels[nodeId]] > 1
              ? invocationIdentity(nodeId, labels[nodeId])
              : undefined,
          nodeType: topology.nodeTypes[nodeId] || runtimeNode?.nodeType || "step",
          isAgent: agentNodeIds.has(nodeId),
          status: runtimeNode?.status,
          error: runtimeNode?.error,
          startedAt: runtimeNode?.startedAt || undefined,
          endedAt: runtimeNode?.endedAt || undefined,
          runningElapsedSeconds: runtimeNode?.runningElapsedSeconds ?? 0,
          declaration,
          instructionLine,
          onOpen: openCallbacks[nodeId],
        },
      };
    });
    return nodes;
  }, [
    agentNodeIds,
    layout.positions,
    openCallbacks,
    runNodes,
    runTopology,
    selectedNodeId,
    topology,
    workflow,
  ]);

  return (
    <ReactFlow
      className="[&_.react-flow__edge-path]:stroke-[#87938d] [&_.react-flow__edge-path]:[stroke-width:1.4] [&_.react-flow__arrowhead_polyline]:fill-[#87938d] [&_.react-flow__arrowhead_polyline]:stroke-[#87938d]"
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
      onPaneClick={onClearNode}
      proOptions={{ hideAttribution: true }}
    >
      {topLeftPanel && (
        <Panel
          position="top-left"
          className="dag-panel dag-runs-panel nodrag nopan nowheel m-3.5 flex items-start gap-2"
        >
          {topLeftPanel}
        </Panel>
      )}
      {bottomRightPanel && (
        <Panel
          position="bottom-right"
          className="dag-panel dag-actions-panel nodrag nopan m-3.5 rounded-[9px] border border-line bg-[rgba(255,255,255,.96)] p-[7px] shadow-[0_6px_20px_rgba(20,31,26,.1)]"
        >
          {bottomRightPanel}
        </Panel>
      )}
      <Background
        color={isCurrentWorkflow ? "#dfe4e1" : "rgba(255,255,255,.06)"}
        variant={BackgroundVariant.Dots}
        gap={isCurrentWorkflow ? 18 : 24}
        size={isCurrentWorkflow ? 2.5 : 1}
      />
      <Controls
        className="overflow-hidden rounded-lg border! border-line! bg-white! shadow-[0_4px_14px_rgba(20,31,26,.08)]! [&_.react-flow__controls-button]:border-b-line! [&_.react-flow__controls-button]:bg-white! [&_.react-flow__controls-button]:fill-secondary! [&_.react-flow__controls-button:hover]:bg-[#f1f4f2]!"
        showInteractive={false}
      />
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
    left.selectedNodeId === right.selectedNodeId &&
    left.onClearNode === right.onClearNode &&
    left.onOpenNode === right.onOpenNode &&
    sameRunNodeState(left.runNodes, right.runNodes),
);
