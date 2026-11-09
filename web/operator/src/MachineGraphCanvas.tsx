import { useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  BaseEdge,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  getViewportForBounds,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import type { MachineSpec } from "./machine";

const WIDTH = 210;
const HEIGHT = 88;
type StateNode = Node<
  { name: string; current: boolean; initial: boolean; final: boolean },
  "machineState"
>;
type Arc = Edge<{ back: boolean; lane: number }, "machineArc">;

function StateCard({ data }: NodeProps<StateNode>) {
  return (
    <div
      data-current={data.current}
      className={`machine-state flex h-full flex-col justify-center gap-2 rounded border px-4 ${data.current ? "border-acid bg-acid/10 text-ink ring-1 ring-acid" : "border-line bg-panel text-ink"}`}
    >
      <Handle type="target" position={Position.Left} id="in" isConnectable={false} />
      <Handle
        type="target"
        position={Position.Bottom}
        id="back-in"
        style={{ left: "75%" }}
        isConnectable={false}
      />
      <strong className="text-sm">{data.name}</strong>
      <span className="text-xs text-secondary">
        {[data.current && "Current", data.initial && "Initial", data.final && "Final"]
          .filter(Boolean)
          .join(" · ")}
      </span>
      <Handle type="source" position={Position.Right} id="out" isConnectable={false} />
      <Handle
        type="source"
        position={Position.Bottom}
        id="back-out"
        style={{ left: "25%" }}
        isConnectable={false}
      />
    </div>
  );
}

/** Cubic arcs, including genuine self-loops; never route cycles through the DAG ranker. */
function MachineArc(props: EdgeProps<Arc>) {
  const { sourceX: x, sourceY: y, targetX: tx, targetY: ty, data } = props;
  const lift = 90 + (data?.lane ?? 0) * 24;
  const [path, labelX, labelY] = data?.back
    ? [
        `M ${x},${y} C ${x - 80},${y + lift} ${tx + 80},${ty + lift} ${tx},${ty}`,
        (x + tx) / 2,
        Math.max(y, ty) + lift * 0.75,
      ]
    : [
        `M ${x},${y} C ${x + 60},${y - 38 - (data?.lane ?? 0) * 24} ${tx - 60},${ty - 38 - (data?.lane ?? 0) * 24} ${tx},${ty}`,
        (x + tx) / 2,
        (y + ty) / 2 - 28.5 - (data?.lane ?? 0) * 18,
      ];
  return (
    <BaseEdge
      {...props}
      path={path}
      labelX={labelX}
      labelY={labelY}
      labelStyle={{ fill: "var(--color-ink)", fontSize: 11 }}
      labelBgStyle={{ fill: "var(--color-panel)" }}
      labelBgPadding={[7, 4]}
    />
  );
}
const nodeTypes = { machineState: StateCard };
const edgeTypes = { machineArc: MachineArc };

export function MachineGraphCanvas({
  spec,
  state,
}: {
  spec: MachineSpec;
  state?: string | null;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ width: number; height: number }>();
  useLayoutEffect(() => {
    const element = container.current!;
    const measure = () => {
      const { width, height } = element.getBoundingClientRect();
      setSize({ width, height });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const graph = useMemo(() => {
    const links = Object.entries(spec.states).flatMap(([source, declaration]) => [
      ...declaration.transitions.map((t) => ({ source, target: t.target, label: t.event })),
      ...declaration.timers.map((t) => ({
        source,
        target: t.target,
        label: `after ${t.delay_seconds}s`,
      })),
    ]);
    const ranks = new Map([[spec.initial, 0]]);
    const queue = [spec.initial];
    for (let i = 0; i < queue.length; i++) {
      for (const link of links) {
        if (link.source === queue[i] && !ranks.has(link.target)) {
          ranks.set(link.target, ranks.get(link.source)! + 1);
          queue.push(link.target);
        }
      }
    }
    const rows = new Map<number, number>();
    const nodes: StateNode[] = Object.entries(spec.states).map(([name, declaration]) => {
      const column = ranks.get(name) ?? 0;
      const row = rows.get(column) ?? 0;
      rows.set(column, row + 1);
      return {
        id: name,
        type: "machineState",
        position: { x: column * 370, y: row * 250 },
        width: WIDTH,
        height: HEIGHT,
        style: { width: WIDTH, height: HEIGHT },
        data: {
          name,
          current: name === state,
          initial: name === spec.initial,
          final: declaration.final,
        },
      };
    });
    const lanes = new Map<string, number>();
    const edges: Arc[] = links.map((link, index) => {
      const back = (ranks.get(link.target) ?? 0) <= (ranks.get(link.source) ?? 0);
      const key = JSON.stringify([link.source, link.target]);
      const lane = lanes.get(key) ?? 0;
      lanes.set(key, lane + 1);
      return {
        ...link,
        id: `machine-${index}`,
        type: "machineArc",
        data: { back, lane },
        sourceHandle: back ? "back-out" : "out",
        targetHandle: back ? "back-in" : "in",
        ariaLabel: `${link.source} to ${link.target}: ${link.label}`,
        markerEnd: { type: MarkerType.ArrowClosed, color: "var(--color-secondary)" },
        style: { stroke: "var(--color-secondary)", strokeWidth: 1.5 },
      };
    });
    return {
      nodes,
      edges,
      bounds: {
        x: -70,
        y: -40,
        width: Math.max(0, ...nodes.map((n) => n.position.x)) + WIDTH + 140,
        height: Math.max(0, ...nodes.map((n) => n.position.y)) + HEIGHT + 200,
      },
    };
  }, [spec, state]);
  const viewport =
    size && size.width > 0 && size.height > 0
      ? getViewportForBounds(graph.bounds, size.width, size.height, 0.1, 1, 0.1)
      : undefined;
  // A measured container and known card sizes yield a final viewport before first paint.
  return (
    <div ref={container} className="h-full min-h-0 w-full" aria-label="Machine state graph">
      {viewport && (
        <ReactFlow
          nodes={graph.nodes}
          edges={graph.edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          defaultViewport={viewport}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          minZoom={0.1}
          maxZoom={2}
        >
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      )}
    </div>
  );
}
