import type { JsonSchema, JsonValue } from "./stepInterface";
import type { RunSnapshotMsg } from "./model";

export type { JsonSchema, JsonValue } from "./stepInterface";
export type MachineKey = JsonValue[];
export interface MachineSpec {
  schema_version: "avalanche.machine/v1";
  name: string;
  input_schema: JsonSchema;
  key_fields: string[];
  initial: string;
  states: Record<string, MachineState>;
  work_graphs: Record<string, MachineWorkGraph>;
}
export interface MachineState {
  work: string;
  initial: boolean;
  final: boolean;
  defer: string[];
  transitions: { event: string; target: string; payload_schema: JsonSchema | null }[];
  timers: { delay_seconds: number; target: string }[];
}
export interface MachineWorkGraph {
  node_ids: string[];
  graph: Record<string, string[]>;
  node_slugs: Record<string, string>;
  node_types: Record<string, string>;
  node_policies: Record<string, { retries: number; on_uncertain: string }>;
  node_waits: Record<string, JsonValue>;
  map_specs?: Record<string, JsonValue>;
  node_emits: Record<string, string[]>;
  node_output_schemas?: Record<string, JsonSchema>;
  emits: string[];
  output_node_id: string | null;
  output_schema: JsonSchema | null;
}
/** Public machine fields use the HTTP model's spelling, not protobuf camel case. */
export interface MachineTransition {
  from: string;
  to: string | null;
  event: string | null;
  source: "internal" | "external" | "timer" | "cancel";
  visit: number;
}
export interface MachineInstance {
  machine: string;
  key: MachineKey;
  run_id: string;
  deployment_id: string;
  status: string;
  state: string | null;
  visit: number;
  version: 1;
  accepts: string[];
  /** Absolute Unix seconds, never a relative delay. */
  due: number | null;
  finished: boolean;
  /** Host-resolved definition for this run (pinned for deployment-based hosts). */
  machine_spec?: MachineSpec;
  /** Local operator extension; receipt admission is distinct from later event facts. */
  pending_events?: { event: string; receipt_id: string; payload: JsonValue }[];
  receipts?: MachineEventReceipt[];
}
export interface MachineVisitNode {
  state: string;
  kind: string;
  deps: string[];
  g: number;
  attempts: number;
}
export interface MachineVisit {
  /** Host-provided bounded snapshot: this run ID, only this visit's qualified
   * nodes/topology/expansions, retained I/O/trace tokens, scoped log token and seal.
   * Never synthesize it from the status-only nodes below. Absent only when the
   * visit's history expired (`disposition: "expired"`). */
  snapshot?: RunSnapshotMsg;
  visit: number;
  state: string;
  entered_at: number | null;
  left_at: number | null;
  /** `expired`: the run's retention policy removed this visit's history and artifacts. */
  disposition: "current" | "retained" | "archived" | "indeterminate" | "expired";
  transition: MachineTransition | null;
  nodes: Record<string, MachineVisitNode>;
  /** Local operator extension: catalog revision that actually executed this visit. */
  catalog_revision?: string;
}
export interface MachineVisitEvent {
  tseq: number;
  at: number;
  status: "accepted" | "consumed" | "ignored" | "dropped";
  event: string | null;
  receipt_id: string | null;
  source: "external" | "internal";
}
export interface MachineEventReceipt {
  receipt_id: string;
  key: string;
  accepted_at: number;
  status: "accepted" | "ignored";
  duplicate: boolean;
}
export interface MachinePage<T> {
  items: T[];
  /** Even an empty page can have a continuation. */
  next_cursor: string | null;
}
export interface MachinePageRequest {
  cursor?: string;
  limit?: number;
}
