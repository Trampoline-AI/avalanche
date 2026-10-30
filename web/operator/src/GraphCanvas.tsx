import {
  memo,
  type CSSProperties,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ArrowDown, ArrowRight, Bot, FlaskConical, ListFilter } from "lucide-react";

import {
  Background,
  BackgroundVariant,
  BaseEdge,
  Controls,
  Handle,
  Panel,
  Position,
  ReactFlow,
  useReactFlow,
  useStore,
  useViewport,
  type Edge,
  type EdgeProps,
  type FitViewOptions,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";

import type {
  FlowInfoMsg,
  NodeSnapshotMsg,
  RunInputMsg,
  TriggerKind,
  WorkflowTopologyMsg,
} from "./model";
import {
  AgentDeclaration as AgentDeclarationSchema,
  type FieldDeclaration,
} from "./generated/agentSchemas";
import { isUnknownRecord } from "./guards";
import { decodeClassifierDeclaration, decodeEvaluationDeclaration } from "./classifier";
import { decodeStepInterface } from "./stepInterface";
import type { EvaluationRecord } from "./evaluations";
import { formatElapsedSeconds } from "./elapsedDuration";
import { CompositeSummary } from "./CompositeSummary";
import { arrangeGraph, createGraphLayout, type RoutedEdgeData } from "./graphLayout";
import {
  highlightConnectedEdges,
  INPUT_EDGE_MARKER,
  INPUT_EDGE_MARKER_CONNECTED,
} from "./graphSelection";
import { useGraphOrientation } from "./useGraphOrientation";
import { ClaudeLogo, isClaudeRuntime } from "./ClaudeLogo";
import { RunStatus, statusTone, statusToneColor } from "./RunStatus";
import {
  INPUT_NODE_ID,
  decodeWorkflowInputDeclaration,
  runInputFields,
  runTriggerLabel,
  scheduledInputNote,
  workflowInputFields,
  type RunInputField,
  type WorkflowInputField,
} from "./workflowInput";
import { InputPopover } from "./InputPopover";
import type { InputDraft } from "./useInputDraft";

interface FieldMetadata {
  name: string;
  type?: string;
  description?: string;
}
export interface SkillMetadata {
  name: string;
  /** What an Agent Skill does and when to use it; PredictRLM skills have none. */
  description?: string;
  instructions: string;
  packages: string[];
  modules: string[];
  files: string[];
}

export interface ToolMetadata {
  name: string;
  sourceCode?: string;
}

export interface AgentFieldSchemas {
  inputs: FieldMetadata[];
  outputs: FieldMetadata[];
}

export interface AgentDeclaration extends AgentFieldSchemas {
  instructions: string;
  /** The runtime the step runs on, such as "predict_rlm", "claude_agent" or "openai_agent". */
  runtimeKind: string;
  model?: unknown;
  runtime?: unknown;
  skills: SkillMetadata[];
  tools: ToolMetadata[];
}

interface CardData extends Record<string, unknown> {
  label: string;
  nodeType: string;
  isAgent: boolean;
  /** The agent runs on ClaudeAgent, the Claude Agent SDK runtime. */
  isClaude: boolean;
  isClassifier: boolean;
  classifierSummary?: string;
  evaluationCount?: number;
  evaluationDeclarationError?: boolean;
  composites?: Record<string, number>;
  identity?: string;
  status?: string;
  error?: string;
  startedAt?: number;
  endedAt?: number;
  runningElapsedSeconds?: number;
  declaration?: AgentFieldSchemas;
  instructionLine?: string;
  onOpen: () => void;
  inspectionDisabled?: boolean;
}

interface InputDotData extends Record<string, unknown> {
  typeName?: string;
  fields: WorkflowInputField[];
  /** Editable draft outside a run. */
  draft?: InputDraft;
  /** The values a run used, or "unavailable" when it has none to show. */
  runValues?: RunInputField[] | "unavailable";
  trigger?: TriggerKind;
  cron?: string;
  /** What scheduled runs start with; only for a cron workflow's definition. */
  scheduleNote?: string;
  error?: string;
  inspectionDisabled?: boolean;
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

/** Validate a published agent declaration; malformed declarations throw. */
export function parseAgentDeclaration(raw: string | undefined): AgentDeclaration | undefined {
  if (!raw) return undefined;
  const declaration = AgentDeclarationSchema.parse(JSON.parse(raw));
  const field = ({ name, annotation, description }: FieldDeclaration): FieldMetadata => ({
    name,
    type: annotation,
    description,
  });
  const signature = {
    instructions: declaration.signature.instructions,
    inputs: declaration.signature.inputs.map(field),
    outputs: declaration.signature.outputs.map(field),
  };
  const tool = ({ name, source_code }: { name: string; source_code: string }) => ({
    name,
    sourceCode: source_code || undefined,
  });
  const { runtime } = declaration;
  const stepSkills = declaration.skills.map(({ name, description, instructions, files }) => ({
    name,
    description,
    instructions,
    packages: [],
    modules: [],
    files,
  }));
  if (runtime.kind === "claude_agent") {
    return {
      ...signature,
      runtimeKind: runtime.kind,
      model: { main: { source: "step override", identity: runtime.model } },
      runtime: { ...runtime.settings, builtin_tools: runtime.builtin_tools },
      skills: stepSkills,
      tools: declaration.tools.map(tool),
    };
  }
  if (runtime.kind === "openai_agent") {
    return {
      ...signature,
      runtimeKind: runtime.kind,
      model: { main: { source: "step override", identity: runtime.model } },
      runtime: { ...runtime.settings, builtin_tools: runtime.builtin_tools },
      skills: stepSkills,
      tools: declaration.tools.map(tool),
    };
  }
  return {
    ...signature,
    runtimeKind: runtime.kind,
    model: runtime.models,
    runtime: runtime.settings,
    // The step's Agent Skills, then PredictRLM's own.
    skills: [
      ...stepSkills,
      ...runtime.skills.map(({ name, instructions, packages, modules }) => ({
        name,
        instructions,
        packages,
        modules,
        files: [],
      })),
    ],
    // Skill tools first, then the step's own, as the agent sees them.
    tools: [...runtime.skill_tools, ...declaration.tools].map(tool),
  };
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

export const NodeDuration = memo(
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
        title={`${elapsedSeconds.toFixed(1)} seconds`}
      >
        {formatElapsedSeconds(elapsedSeconds)}
      </span>
    );
  },
);
NodeDuration.displayName = "NodeDuration";

const WorkflowNodeCard = memo(({ data, selected }: NodeProps<Node<CardData>>) => {
  const { zoom } = useViewport();
  const isCompact = zoom < NODE_DETAIL_ZOOM_THRESHOLD;
  const hasEvaluations = data.evaluationCount !== undefined || data.evaluationDeclarationError;
  const hasEvaluationRow = hasEvaluations || Boolean(data.composites);
  const designationClass = data.isClassifier
    ? "node-classifier"
    : data.isAgent
      ? `node-agent${data.isClaude ? " node-claude" : ""}`
      : "";
  const state = data.status ?? "pending";
  const stateColor = statusToneColor[statusTone(state)];
  return (
    <article
      className={`node-card ${isCompact ? `node-card--compact ${hasEvaluationRow ? "min-h-[130px]" : "min-h-[100px]"} justify-center gap-0 px-4 py-3` : "min-h-[130px] gap-2 p-4"} relative flex w-[360px] cursor-pointer flex-col items-stretch rounded border text-left motion-reduce:transition-none ${designationClass}`}
      data-node-state={state}
      style={
        {
          "--node-state-color": stateColor,
          outline: selected ? `${2 / zoom}px solid var(--color-acid)` : undefined,
          outlineOffset: selected ? "2px" : undefined,
        } as CSSProperties
      }
      data-node-kind={data.isClassifier ? "classifier" : data.isAgent ? "agent" : "standard"}
      aria-disabled={data.inspectionDisabled || undefined}
    >
      {data.isClaude && (
        <ClaudeLogo
          title="Runs on the Claude Agent SDK"
          className={`node-claude-logo pointer-events-none absolute z-[3] transition-[width,height] duration-150 ease-out motion-reduce:transition-none ${isCompact ? "top-3 left-3 size-7" : "top-[17px] left-4 size-4"}`}
        />
      )}
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
      <Handle
        id="target-top"
        className="node-handle pointer-events-none size-px! min-h-0! min-w-0! border-0! bg-transparent! opacity-0"
        type="target"
        position={Position.Top}
        isConnectable={false}
      />
      <Handle
        id="target-right"
        className="node-handle pointer-events-none size-px! min-h-0! min-w-0! border-0! bg-transparent! opacity-0"
        type="target"
        position={Position.Right}
        isConnectable={false}
      />
      <button
        type="button"
        className="node-card-action absolute inset-0 z-[2] cursor-pointer rounded-[inherit] border-0 bg-transparent p-0 focus-visible:outline-3 focus-visible:outline-offset-3 focus-visible:outline-acid"
        onClick={data.onOpen}
        disabled={data.inspectionDisabled}
        aria-label={`Inspect ${data.label}${data.identity ? ` ${data.identity}` : ""}`}
        aria-description={
          data.evaluationDeclarationError
            ? "Evaluation declaration unavailable"
            : data.evaluationCount !== undefined
              ? `${data.evaluationCount} evaluation ${data.evaluationCount === 1 ? "metric" : "metrics"} configured`
              : undefined
        }
      />
      <header
        className={`node-header relative ${
          isCompact
            ? "grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)] grid-rows-[1fr_auto_1fr] items-center gap-0 pr-0 text-center"
            : "flex min-h-10 flex-col items-start gap-1"
        }`}
      >
        <span
          className={`${data.isClassifier || data.isAgent ? `mb-1 inline-flex items-center gap-1 rounded px-1.5 py-0.5 ${data.isClassifier ? "node-classifier-identity bg-classifier-light text-classifier" : data.isClaude ? `node-agent-identity bg-claude/10 text-claude ${isCompact ? "" : "ml-6"}` : "node-agent-identity bg-agent/10 text-agent"}` : "node-card-meta text-secondary"} node-kicker font-mono ${isCompact && data.isAgent ? "text-sm" : "text-[8px]"} tracking-[.12em] uppercase`}
        >
          {data.isClassifier ? (
            <ListFilter aria-hidden="true" className="size-3" />
          ) : data.isAgent ? (
            <Bot aria-hidden="true" className={isCompact ? "size-4" : "size-3"} />
          ) : null}
          {data.isClassifier ? "Classifier" : data.isAgent ? "Agent" : data.nodeType}
        </span>
        <strong
          className={`node-title block self-stretch font-mono ${isCompact ? "text-xl" : "pr-[76px] text-sm"} leading-tight text-ink`}
        >
          <RunStatus status={state} glyphOnly /> {data.label}
          {data.identity && ` ${data.identity}`}
        </strong>
        {data.classifierSummary && (
          <span className="node-classifier-summary mt-1 min-w-0 self-stretch text-[9px] leading-relaxed text-classifier [overflow-wrap:anywhere]">
            {data.classifierSummary}
          </span>
        )}
        {data.instructionLine && (
          <span
            className="node-card-meta node-instruction-line min-w-0 self-stretch overflow-hidden text-ellipsis line-clamp-2 font-mono text-[9px] leading-[1.35] text-secondary"
            title={data.instructionLine}
          >
            {data.instructionLine}
          </span>
        )}
        {data.status && (
          <span
            className={`node-card-meta node-status absolute top-[19px] right-0 font-mono text-[8px] uppercase text-[color:var(--node-state-color)]`}
          >
            {data.status}
          </span>
        )}
      </header>
      {hasEvaluationRow && (
        <div
          className={`node-evaluation-row flex min-w-0 items-center gap-2 ${isCompact ? "absolute inset-x-4 bottom-3" : "self-stretch"}`}
        >
          {hasEvaluations && (
            <span
              className={`node-evaluation-badge inline-flex shrink-0 items-center gap-1 rounded border border-classifier/20 bg-classifier-light px-1.5 py-0.5 font-mono text-classifier ${isCompact ? "text-sm" : "text-[9px]"}`}
              aria-label={
                data.evaluationDeclarationError
                  ? "Evaluation declaration unavailable"
                  : `${data.evaluationCount} evaluation metrics configured`
              }
              title={
                data.evaluationDeclarationError
                  ? "Evaluation declaration unavailable"
                  : "Configured operator-only evaluations; not a run result"
              }
            >
              <FlaskConical aria-hidden="true" className={isCompact ? "size-4" : "size-3"} />
              {data.evaluationDeclarationError
                ? isCompact
                  ? "!"
                  : "Evaluations · unavailable"
                : isCompact
                  ? data.evaluationCount
                  : `Evaluations · ${data.evaluationCount}`}
            </span>
          )}
          {data.composites && (
            <CompositeSummary
              composites={data.composites}
              compact={isCompact}
              maxVisible={3}
              className={`node-composite-summary flex min-w-0 flex-1 items-center justify-end gap-1 text-right font-mono ${isCompact ? "text-sm" : "text-[9px]"}`}
            />
          )}
        </div>
      )}
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
              : "gap-6 border-t border-line pt-2.5"
          }`}
        >
          <section
            className="node-fields node-inputs min-w-0 overflow-hidden [&>small]:mb-1.5 [&>small]:block [&>small]:font-mono [&>small]:text-[7px] [&>small]:tracking-[.08em] [&>small]:text-muted [&>small]:uppercase"
            aria-label="Inputs"
          >
            <small>Inputs</small>
            {data.declaration.inputs.map((field) => (
              <span
                className="field node-field mt-[3px] flex min-h-3.5 min-w-0 items-start justify-start gap-1 font-mono text-[7px]/[1.35] text-field-ink [&>code]:min-w-0 [&>code]:[overflow-wrap:break-word] [&>code]:text-[7px] [&>code]:text-muted"
                key={`input-${field.name}`}
              >
                <span
                  className="node-field-name max-w-full shrink-0 truncate"
                  title={field.name}
                >
                  {field.name}
                </span>
                {field.type && <code title={field.type}>{field.type}</code>}
              </span>
            ))}
          </section>
          <section
            className="node-fields node-outputs min-w-0 overflow-hidden text-right [&>small]:mb-1.5 [&>small]:block [&>small]:font-mono [&>small]:text-[7px] [&>small]:tracking-[.08em] [&>small]:text-muted [&>small]:uppercase"
            aria-label="Outputs"
          >
            <small>Outputs</small>
            {data.declaration.outputs.map((field) => (
              <span
                className="field node-field mt-[3px] flex min-h-3.5 min-w-0 items-start justify-end gap-1 font-mono text-[7px]/[1.35] text-field-ink [&>code]:min-w-0 [&>code]:[overflow-wrap:break-word] [&>code]:text-[7px] [&>code]:text-muted"
                key={`output-${field.name}`}
              >
                <span
                  className="node-field-name max-w-full shrink-0 truncate"
                  title={field.name}
                >
                  {field.name}
                </span>
                {field.type && <code title={field.type}>{field.type}</code>}
              </span>
            ))}
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

/**
 * The run input as a dot before the workflow's first steps, so it never reads as a
 * step. Clicking it opens its fields over the graph, editable outside a run.
 */
const WorkflowInputDot = memo(({ data }: NodeProps<Node<InputDotData>>) => {
  const { zoom } = useViewport();
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  const note = data.trigger ? runTriggerLabel(data.trigger) : data.scheduleNote;
  return (
    <div
      className="node-input relative grid size-7 place-items-center"
      data-node-kind="input"
      data-open={open || undefined}
      data-error={data.error ? true : undefined}
      data-run-trigger={data.trigger}
      data-cron={data.cron}
    >
      <button
        type="button"
        className="node-card-action absolute inset-0 z-[2] cursor-pointer rounded-full border-0 bg-transparent p-0 focus-visible:outline-3 focus-visible:outline-offset-3 focus-visible:outline-acid"
        onClick={() => setOpen((value) => !value)}
        disabled={data.inspectionDisabled}
        aria-expanded={open}
        aria-label={`Workflow input${data.typeName ? ` ${data.typeName}` : ""}`}
      />
      <span aria-hidden="true" className="node-input-dot size-6 rounded-full" />
      <span
        aria-hidden="true"
        className="node-input-label pointer-events-none absolute top-full left-1/2 mt-1 font-mono text-[11px] whitespace-nowrap text-secondary"
        // Readable at any canvas zoom, like the popup.
        style={{
          transform: `translateX(-50%) scale(${1 / zoom})`,
          transformOrigin: "top center",
        }}
      >
        inputs
      </span>
      {open && (
        // Counter the canvas zoom so the popup reads at its normal size.
        <div
          className="absolute top-0 left-0 z-[3]"
          style={{ transform: `scale(${1 / zoom})`, transformOrigin: "top left" }}
        >
          <InputPopover
            typeName={data.typeName}
            fields={data.fields}
            draft={data.draft}
            runValues={data.runValues}
            note={note}
            error={data.error}
            onClose={close}
          />
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
    </div>
  );
});
WorkflowInputDot.displayName = "WorkflowInputDot";

/** Diamond heads for input edges; edges refer to them by ID (see graphSelection). */
function InputEdgeMarkers() {
  return (
    <svg aria-hidden="true" className="pointer-events-none absolute size-0 overflow-hidden">
      <defs>
        {[
          [INPUT_EDGE_MARKER, "var(--color-ink)"],
          [INPUT_EDGE_MARKER_CONNECTED, "var(--color-acid)"],
        ].map(([id, color]) => (
          <marker
            key={id}
            id={id}
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="12"
            markerHeight="12"
            markerUnits="userSpaceOnUse"
            orient="auto"
          >
            <path d="M0 5 L5 0 L10 5 L5 10 Z" style={{ fill: color }} />
          </marker>
        ))}
      </defs>
    </svg>
  );
}

// Paths come from the layout, which routes every edge around cards and other edges.
const RoutedEdge = memo(
  ({ data, markerEnd, style }: EdgeProps<Edge<RoutedEdgeData, "routed">>) => {
    if (data === undefined) throw new Error("Routed edge is missing its path");
    return <BaseEdge path={data.path} markerEnd={markerEnd} style={style} />;
  },
);
RoutedEdge.displayName = "RoutedEdge";

const NODE_TYPES = { workflow: WorkflowNodeCard, workflowInput: WorkflowInputDot };
const NODE_ORIGIN: [number, number] = [0, 0.5];
const EDGE_TYPES = { routed: RoutedEdge };
const FIT_VIEW_OPTIONS = { padding: 0.24 };
// Leave room for the floating timeline above a top-to-bottom graph.
const VERTICAL_FIT_VIEW_OPTIONS: FitViewOptions = {
  padding: { top: "160px", bottom: "90px", left: "40px", right: "80px" },
};

function FitGraphOnWidthChange({ options }: { options: FitViewOptions }) {
  const width = useStore((state) => state.width);
  const { fitView } = useReactFlow();
  const previousWidth = useRef(width);

  useEffect(() => {
    const previous = previousWidth.current;
    previousWidth.current = width;
    // Initial fitting belongs to React Flow. Re-fit only after its resize observer
    // has committed a changed canvas width (timeline, inspector or window resize).
    if (previous <= 0 || width <= 0 || previous === width) return;
    const frame = requestAnimationFrame(() => void fitView(options));
    return () => cancelAnimationFrame(frame);
  }, [width, fitView, options]);

  return null;
}

function invocationIdentity(nodeId: string, label: string): string {
  const suffix = nodeId.startsWith(`${label}_`) ? nodeId.slice(label.length + 1) : "";
  return suffix && /^\d+$/.test(suffix) ? `#${suffix}` : nodeId;
}

type TopologyView = Pick<
  WorkflowTopologyMsg,
  | "nodeIds"
  | "graph"
  | "nodeTypes"
  | "displayNames"
  | "agentInstructionLines"
  | "standardStepDocstringLines"
  | "classifierMetadataJson"
  | "evaluationMetadataJson"
  | "stepInterfaceJson"
  | "inputDeclarationJson"
>;

interface GraphCanvasProps {
  workflow?: FlowInfoMsg;
  runTopology?: WorkflowTopologyMsg;
  runNodes?: NodeSnapshotMsg[];
  /** The values the displayed run started with. */
  runInput?: RunInputMsg;
  /** The editable input draft, outside a run. */
  inputDraft?: InputDraft;
  /** How the displayed run started, such as "manual" or "scheduled". */
  runTriggeredBy?: TriggerKind;
  evaluations?: Record<string, EvaluationRecord>;
  bottomRightPanel?: ReactNode;
  selectedNodeId?: string;
  onClearNode?: () => void;
  onOpenNode: (nodeId: string) => void;
  inspectionDisabled?: boolean;
}

function GraphCanvasView({
  workflow,
  runTopology,
  runNodes = [],
  runInput,
  inputDraft,
  runTriggeredBy,
  evaluations,
  bottomRightPanel,
  selectedNodeId,
  onClearNode,
  onOpenNode,
  inspectionDisabled = false,
}: GraphCanvasProps) {
  const { containerRef, orientation, toggleOrientation } = useGraphOrientation();
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
      standardStepDocstringLines: workflow.standardStepDocstringLines,
      classifierMetadataJson: workflow.classifierMetadataJson,
      evaluationMetadataJson: workflow.evaluationMetadataJson,
      stepInterfaceJson: workflow.stepInterfaceJson,
      inputDeclarationJson: workflow.inputDeclarationJson,
    };
  }, [runTopology, workflow]);
  const rawInputDeclaration = topology?.inputDeclarationJson ?? "";
  const inputDeclaration = useMemo(() => {
    try {
      return { declaration: decodeWorkflowInputDeclaration(rawInputDeclaration) };
    } catch {
      return { error: "Input declaration unavailable" };
    }
  }, [rawInputDeclaration]);
  const hasInputNode = rawInputDeclaration !== "";
  // The input dot leads the graph, with edges into the workflow's first steps.
  const topologyNodeIds = useMemo(
    () => (topology && hasInputNode ? [INPUT_NODE_ID, ...topology.nodeIds] : topology?.nodeIds),
    [hasInputNode, topology],
  );
  const topologyGraph = useMemo(
    () =>
      topology && hasInputNode
        ? {
            ...topology.graph,
            [INPUT_NODE_ID]: {
              children: topology.nodeIds.filter(
                (nodeId) =>
                  !Object.values(topology.graph).some(({ children }) =>
                    children.includes(nodeId),
                  ),
              ),
            },
          }
        : topology?.graph,
    [hasInputNode, topology],
  );
  const [nodeMeasurements, setNodeMeasurements] = useState<
    ReadonlyMap<string, { width: number; height: number }>
  >(() => new Map());
  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      setNodeMeasurements((current) => {
        let next: Map<string, { width: number; height: number }> | undefined;
        for (const change of changes) {
          if (change.type !== "dimensions" || !change.dimensions) continue;
          const previous = current.get(change.id);
          if (
            previous?.width === change.dimensions.width &&
            previous?.height === change.dimensions.height
          ) {
            continue;
          }
          next ??= new Map(current);
          next.set(change.id, change.dimensions);
        }
        if (!next) return current;
        const nodeIds = new Set(topologyNodeIds);
        for (const nodeId of next.keys()) {
          if (!nodeIds.has(nodeId)) next.delete(nodeId);
        }
        return next;
      });
    },
    [topologyNodeIds],
  );
  const layout = useMemo(
    () =>
      topologyNodeIds && topologyGraph
        ? createGraphLayout({ nodeIds: topologyNodeIds, graph: topologyGraph }, orientation)
        : { edges: [], layers: [], chains: {} },
    [topologyGraph, topologyNodeIds, orientation],
  );
  const { positions, paths } = useMemo(
    () => arrangeGraph(layout, nodeMeasurements, orientation),
    [layout, nodeMeasurements, orientation],
  );
  const edges = useMemo(
    () =>
      highlightConnectedEdges(
        layout.edges.map((edge) => ({ ...edge, data: { path: paths[edge.id] } })),
        selectedNodeId,
      ),
    [layout.edges, paths, selectedNodeId],
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
  const classifierMetadata = topology?.classifierMetadataJson;
  const classifierCards = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(classifierMetadata ?? {}).map(([nodeId, raw]) => {
          try {
            const declaration = decodeClassifierDeclaration(raw);
            const counts = { choice: 0, noul: 0, score: 0 };
            if (declaration.questions !== null) {
              for (const question of Object.values(declaration.questions)) {
                counts[question.type] += 1;
              }
            }
            const kinds = [
              counts.choice ? `${counts.choice} Choice` : "",
              counts.noul ? `${counts.noul} Noul` : "",
              counts.score ? `${counts.score} Score` : "",
            ]
              .filter(Boolean)
              .join(" · ");
            const fields: AgentFieldSchemas = {
              inputs: declaration.step_inputs.map((input) => ({
                name: input.name,
                type: input.type_name,
              })),
              outputs: [{ name: "return", type: declaration.step_output.type_name }],
            };
            return [
              nodeId,
              {
                summary:
                  declaration.questions === null ? "Runtime questions" : `Defaults: ${kinds}`,
                fields,
              },
            ];
          } catch {
            return [nodeId, { summary: "Question definition unavailable", fields: undefined }];
          }
        }),
      ),
    [classifierMetadata],
  );
  const evaluationMetadata = topology?.evaluationMetadataJson;
  const evaluationCards = useMemo<Record<string, { count?: number; error: boolean }>>(
    () =>
      Object.fromEntries(
        Object.entries(evaluationMetadata ?? {}).map(([nodeId, raw]) => {
          try {
            const declaration = decodeEvaluationDeclaration(raw);
            return [nodeId, { count: Object.keys(declaration.metrics).length, error: false }];
          } catch {
            return [nodeId, { count: undefined, error: true }];
          }
        }),
      ),
    [evaluationMetadata],
  );
  const stepInterfaces = topology?.stepInterfaceJson;
  const standardFields = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(stepInterfaces ?? {})
          .filter(
            ([nodeId]) => !agentNodeIds.has(nodeId) && !Object.hasOwn(classifierCards, nodeId),
          )
          .map(([nodeId, raw]) => {
            try {
              const definition = decodeStepInterface(raw);
              const fields: AgentFieldSchemas = {
                inputs: definition.step_inputs.map((input) => ({
                  name: input.name,
                  type: input.type_name,
                })),
                outputs: [{ name: "return", type: definition.step_output.type_name }],
              };
              return [nodeId, fields];
            } catch {
              return [nodeId, undefined];
            }
          }),
      ),
    [agentNodeIds, classifierCards, stepInterfaces],
  );
  const cron = runTopology ? "" : (workflow?.cron ?? "");
  const scheduleNote = runTopology || !workflow ? undefined : scheduledInputNote(workflow);
  const inputCard = useMemo<InputDotData | undefined>(() => {
    if (!hasInputNode) return undefined;
    const { declaration, error } = inputDeclaration;
    const fields = declaration ? workflowInputFields(declaration) : [];
    let runValues: InputDotData["runValues"];
    let valuesError: string | undefined;
    if (runTopology && declaration) {
      try {
        runValues = runInput ? runInputFields(declaration, runInput) : "unavailable";
      } catch {
        valuesError = "Run input unavailable";
      }
    }
    return {
      typeName: declaration?.typeName,
      fields,
      draft: runTopology ? undefined : inputDraft,
      runValues,
      trigger: runTopology ? runTriggeredBy : undefined,
      cron: runTopology ? undefined : cron || undefined,
      scheduleNote,
      error: error ?? valuesError,
      inspectionDisabled,
    };
  }, [
    cron,
    hasInputNode,
    inputDeclaration,
    inputDraft,
    inspectionDisabled,
    runInput,
    runTopology,
    runTriggeredBy,
    scheduleNote,
  ]);
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
        : parseAgentDeclaration(workflow?.agentDeclarationsJson[nodeId]);
      const classifierCard = classifierCards[nodeId];
      const declaration = classifierCard
        ? classifierCard.fields
        : agentNodeIds.has(nodeId)
          ? runTopology
            ? parseAgentFieldSchemas(runTopology.agentFieldSchemasJson[nodeId])
            : agentDeclaration
          : standardFields[nodeId];
      const instructionLine =
        topology.agentInstructionLines[nodeId] ||
        topology.standardStepDocstringLines[nodeId] ||
        (!runTopology ? firstInstructionLine(agentDeclaration?.instructions) : undefined);
      const evaluation = runTopology ? evaluations?.[nodeId] : undefined;
      return {
        id: nodeId,
        selected: nodeId === selectedNodeId,
        type: "workflow",
        position: positions[nodeId],
        measured: nodeMeasurements.get(nodeId),
        data: {
          label: labels[nodeId],
          identity:
            labelCounts[labels[nodeId]] > 1
              ? invocationIdentity(nodeId, labels[nodeId])
              : undefined,
          nodeType: topology.nodeTypes[nodeId] || runtimeNode?.nodeType || "step",
          isAgent: agentNodeIds.has(nodeId),
          isClaude: isClaudeRuntime(
            runTopology ? runTopology.agentRuntimeKinds[nodeId] : agentDeclaration?.runtimeKind,
          ),
          isClassifier: Object.hasOwn(topology.classifierMetadataJson, nodeId),
          classifierSummary: classifierCard?.summary,
          evaluationCount: evaluationCards[nodeId]?.count,
          evaluationDeclarationError: evaluationCards[nodeId]?.error,
          composites:
            evaluation?.status === "completed" ? evaluation.result.composites : undefined,
          status: runtimeNode?.status,
          error: runtimeNode?.error,
          startedAt: runtimeNode?.startedAt || undefined,
          endedAt: runtimeNode?.endedAt || undefined,
          runningElapsedSeconds: runtimeNode?.runningElapsedSeconds ?? 0,
          declaration,
          instructionLine,
          onOpen: openCallbacks[nodeId],
          inspectionDisabled,
        },
      };
    });
    if (!inputCard) return nodes;
    const inputNode: Node<InputDotData> = {
      id: INPUT_NODE_ID,
      selected: selectedNodeId === INPUT_NODE_ID,
      // Its popup opens over the step cards.
      zIndex: 1000,
      type: "workflowInput",
      position: positions[INPUT_NODE_ID],
      measured: nodeMeasurements.get(INPUT_NODE_ID),
      data: inputCard,
    };
    return [inputNode, ...nodes];
  }, [
    inputCard,
    agentNodeIds,
    classifierCards,
    evaluationCards,
    evaluations,
    standardFields,
    inspectionDisabled,
    nodeMeasurements,
    positions,
    openCallbacks,
    runNodes,
    runTopology,
    selectedNodeId,
    topology,
    workflow,
  ]);

  return (
    <div ref={containerRef} className="graph-viewport" data-orientation={orientation}>
      <ReactFlow
        key={orientation}
        nodes={nodes}
        onNodesChange={onNodesChange}
        edges={edges}
        nodeTypes={NODE_TYPES}
        nodeOrigin={NODE_ORIGIN}
        edgeTypes={EDGE_TYPES}
        fitView
        fitViewOptions={
          orientation === "vertical" ? VERTICAL_FIT_VIEW_OPTIONS : FIT_VIEW_OPTIONS
        }
        minZoom={0.25}
        maxZoom={1.8}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={!inspectionDisabled}
        onPaneClick={onClearNode}
        proOptions={{ hideAttribution: true }}
      >
        <InputEdgeMarkers />
        <FitGraphOnWidthChange
          options={orientation === "vertical" ? VERTICAL_FIT_VIEW_OPTIONS : FIT_VIEW_OPTIONS}
        />
        {bottomRightPanel && (
          <Panel
            position="bottom-right"
            className="dag-panel dag-actions-panel nodrag nopan m-3.5 rounded border border-line bg-overlay-panel p-[7px] shadow-[0_6px_20px_var(--color-shadow-2)]"
          >
            {bottomRightPanel}
          </Panel>
        )}
        <Background
          color={isCurrentWorkflow ? "var(--color-line)" : "var(--color-run-dots)"}
          variant={BackgroundVariant.Dots}
          gap={isCurrentWorkflow ? 18 : 24}
          size={isCurrentWorkflow ? 2.5 : 1}
        />
        <Controls
          fitViewOptions={
            orientation === "vertical" ? VERTICAL_FIT_VIEW_OPTIONS : FIT_VIEW_OPTIONS
          }
          aria-label="Navpanel"
          className="navpanel"
          showInteractive={false}
        >
          <div role="group" aria-label="Workflow orientation" className="navpanel-orientation">
            <button
              type="button"
              className="graph-orientation-toggle"
              aria-label={`Switch to ${orientation === "vertical" ? "horizontal" : "vertical"} workflow`}
              title={`${orientation === "vertical" ? "Vertical" : "Horizontal"} layout. Click to switch.`}
              onClick={toggleOrientation}
            >
              {orientation === "vertical" ? (
                <ArrowDown aria-hidden="true" size={17} />
              ) : (
                <ArrowRight aria-hidden="true" size={17} />
              )}
            </button>
          </div>
        </Controls>
      </ReactFlow>
    </div>
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
    left.runInput === right.runInput &&
    left.inputDraft === right.inputDraft &&
    left.runTriggeredBy === right.runTriggeredBy &&
    left.evaluations === right.evaluations &&
    left.bottomRightPanel === right.bottomRightPanel &&
    left.selectedNodeId === right.selectedNodeId &&
    left.onClearNode === right.onClearNode &&
    left.onOpenNode === right.onOpenNode &&
    left.inspectionDisabled === right.inspectionDisabled &&
    sameRunNodeState(left.runNodes, right.runNodes),
);
