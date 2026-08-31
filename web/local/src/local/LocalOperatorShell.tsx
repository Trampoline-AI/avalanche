import { OperatorUi } from "@trampoline-ai/operator-ui";
import type { OperatorApi, OperatorUiHost } from "@trampoline-ai/operator-ui";

const avalancheDiamond = new URL(
  "../../../../docs/assets/brand/avalanche-diamond-3d-1024.png",
  import.meta.url,
).href;

interface LocalOperatorShellProps {
  api: OperatorApi;
  operatorPort?: string;
}

export function LocalOperatorShell({ api, operatorPort = "7433" }: LocalOperatorShellProps) {
  const host: OperatorUiHost = {
    api,
    presentation: {
      brandImageUrl: avalancheDiamond,
      rootLabel: "Local operator",
      unavailableDescription: `No operator process found at port ${operatorPort}`,
      workflowReloadDescription: "Workflow change detected. Scanning...",
    },
  };

  return <OperatorUi host={host} />;
}
