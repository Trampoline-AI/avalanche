import { OperatorConsole } from "@avalanche/operator-console";
import type { OperatorApi, OperatorConsoleHost } from "@avalanche/operator-console";

const avalancheDiamond = new URL(
  "../../../../docs/assets/brand/avalanche-diamond-3d-1024.png",
  import.meta.url,
).href;

interface LocalOperatorShellProps {
  api: OperatorApi;
  operatorPort?: string;
}

export function LocalOperatorShell({ api, operatorPort = "7433" }: LocalOperatorShellProps) {
  const host: OperatorConsoleHost = {
    api,
    presentation: {
      brandImageUrl: avalancheDiamond,
      rootLabel: "Local operator",
      unavailableDescription: `No operator process found at port ${operatorPort}`,
      workflowReloadDescription: "Workflow change detected. Scanning...",
    },
  };

  return <OperatorConsole host={host} />;
}
