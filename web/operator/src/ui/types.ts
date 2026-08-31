import type { OperatorApi } from "../api";

export type OperatorUiSelection =
  { kind: "workflow"; workflowId: string } | { kind: "run"; workflowId: string; runId: string };

export interface OperatorUiPresentation {
  rootLabel: string;
  brandImageUrl: string;
  unavailableDescription: string;
  workflowReloadDescription: string;
}

export interface OperatorUiHost {
  api: OperatorApi;
  presentation: OperatorUiPresentation;
}

export interface OperatorUiNavigation {
  selection: OperatorUiSelection | undefined;
  onSelectionChange: (selection: OperatorUiSelection | undefined) => void;
}

export interface OperatorUiProps {
  host: OperatorUiHost;
  navigation?: OperatorUiNavigation;
}
