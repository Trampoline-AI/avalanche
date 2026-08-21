import type { OperatorApi } from "../api";

export type OperatorConsoleSelection =
  { kind: "workflow"; workflowId: string } | { kind: "run"; workflowId: string; runId: string };

export interface OperatorConsolePresentation {
  rootLabel: string;
  brandImageUrl: string;
  unavailableDescription: string;
  workflowReloadDescription: string;
}

export interface OperatorConsoleHost {
  api: OperatorApi;
  presentation: OperatorConsolePresentation;
}

export interface OperatorConsoleNavigation {
  selection: OperatorConsoleSelection | undefined;
  onSelectionChange: (selection: OperatorConsoleSelection | undefined) => void;
}

export interface OperatorConsoleProps {
  host: OperatorConsoleHost;
  navigation?: OperatorConsoleNavigation;
}
