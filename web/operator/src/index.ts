import "./style.css";

export { GrpcWebOperatorApi } from "./api";
export type {
  AgentEventDescriptorPage,
  AgentEventPageRequest,
  ClassifierEventDescriptorPage,
  ClassifierEventPageRequest,
  LogDescriptorPage,
  LogPageRequest,
  OperatorApi,
  RunLink,
  RunLinks,
  StructuralBaseline,
} from "./api";
export type { RunResultTarget } from "./stepIo";
// The shared result codec, for hosts whose result route returns it (Delta's `/result`).
export { decodeRunResultValue } from "./stepIo";
export type { EvaluationRecord, EvaluationResult } from "./evaluations";
export { OperatorUi } from "./ui/OperatorUi";
export type {
  OperatorUiHost,
  OperatorUiNavigation,
  OperatorUiPresentation,
  OperatorUiProps,
  OperatorUiSelection,
} from "./ui/types";
export { WorkflowWorkspace } from "./WorkflowWorkspace";
export type {
  WorkflowWorkspaceProps,
  WorkspaceDeployment,
  WorkspaceDeployments,
} from "./WorkflowWorkspace";
export * from "./model";
export type * from "./machine";
// Machine run pieces, for hosts composing the run page (the console port).
export {
  MachineRunActions,
  MachineSendPanel,
  machineStateEntries,
  useMachineHistory,
  useMachineInstance,
  useMachineStateEntry,
  useMachineStateEntryDetails,
  useRunWaits,
} from "./MachineWorkspace";
// The run timeline (bottom pane tab beside Logs), for flow and machine runs.
export { RunTimeline } from "./RunTimeline";
export type { RunTimelineProps, TimelineFocus } from "./RunTimeline";
export {
  filterTimeline,
  flowPending,
  flowTimeline,
  machinePending,
  machineTimeline,
} from "./timelineEntries";
export type {
  GraphHighlight,
  MachineStateEntryDetail,
  PendingItem,
  TimelineArc,
  TimelineEntry,
  TimelineFilter,
  TimelineKind,
} from "./timelineEntries";
export type { MachineHistory, MachineReadRefresh } from "./MachineWorkspace";
export {
  LOCAL_LIVE_RUN_STATUSES,
  LOCAL_TERMINAL_RUN_STATUSES,
  runStatusLive,
  runStatusTerminal,
} from "./runLiveness";
export { runKeyLabel } from "./runKey";
// Relative run times on one shared low-frequency clock, and the no-truncation
// priority fit for run rows, for hosts' own run surfaces (the console top bar).
export {
  formatAbsoluteTime,
  formatTimeAgo,
  TIME_AGO_TICK_MS,
  TimeAgo,
  useNow,
} from "./timeAgo";
export type { TimeAgoProps } from "./timeAgo";
export {
  fitPriorityFields,
  priorityFieldVisible,
  textMeasure,
  twoLineWidth,
  useObservedWidth,
  usePriorityFit,
} from "./priorityFit";
export type { PriorityField, PriorityFit, PriorityTextField, TextMeasure } from "./priorityFit";
export { fitRunRow, runSecondaryText, shortRunId } from "./RunListPanel";
export type { MachineGraphView } from "./machineGraph";
