import { GrpcWebOperatorApi, OperatorUi } from "@trampoline-ai/operator-ui";
import type { CatalogSnapshotMsg } from "@trampoline-ai/operator-ui";
import { useState } from "react";

const avalancheDiamond = new URL(
  "../../../../docs/assets/brand/avalanche-diamond-3d-1024.png",
  import.meta.url,
).href;

const DEFAULT_ROOT_LABEL = "Local operator";

class LocalOperatorApi extends GrpcWebOperatorApi {
  constructor(private readonly onCatalogLoaded: (catalog: CatalogSnapshotMsg) => void) {
    super();
  }

  override async getCatalog(signal?: AbortSignal): Promise<CatalogSnapshotMsg> {
    const catalog = await super.getCatalog(signal);
    this.onCatalogLoaded(catalog);
    return catalog;
  }
}

interface LocalOperatorShellProps {
  operatorPort?: string;
}

export function LocalOperatorShell({ operatorPort = "7433" }: LocalOperatorShellProps) {
  const [rootLabel, setRootLabel] = useState(DEFAULT_ROOT_LABEL);
  const [api] = useState(
    () =>
      new LocalOperatorApi((catalog) => {
        setRootLabel(
          catalog.scanTargets.length === 1
            ? catalog.scanTargets[0].targetPath
            : DEFAULT_ROOT_LABEL,
        );
      }),
  );
  const host = {
    api,
    presentation: {
      brandImageUrl: avalancheDiamond,
      rootLabel,
      unavailableDescription: `No operator process found at port ${operatorPort}`,
      workflowReloadDescription: "Workflow change detected. Scanning...",
    },
  };

  return <OperatorUi host={host} />;
}
