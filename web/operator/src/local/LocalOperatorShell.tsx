import type { OperatorApi } from "../api";
import { OperatorConsole } from "../console/OperatorConsole";
import type { OperatorConsoleHost } from "../console/types";

interface LocalOperatorShellProps {
  api: OperatorApi;
  operatorPort?: string;
}

export function LocalOperatorShell({ api, operatorPort = "7433" }: LocalOperatorShellProps) {
  const host: OperatorConsoleHost = {
    api,
    presentation: {
      rootLabel: "Local operator",
      unavailableDescription: `No operator process found at port ${operatorPort}`,
      workflowReloadDescription: "Workflow change detected. Scanning...",
    },
  };

  return <OperatorConsole host={host} />;
}
