# @trampoline-ai/operator-ui

Embeddable React UI for inspecting and controlling Avalanche workflows. The package provides the
workflow graph, run list, run controls, logs, node details, and agent details; the embedding host
owns navigation, the operator connection, and presentation.

## Install

The package is published through GitHub Packages. Configure the scope and an authenticated token
outside source control:

```ini
@trampoline-ai:registry=https://npm.pkg.github.com
//npm.pkg.github.com/:_authToken=${NODE_AUTH_TOKEN}
```

Then install the package with its React peer dependencies:

```bash
pnpm add @trampoline-ai/operator-ui react react-dom
```

## Embed

Import the package stylesheet once, then render `OperatorUi` with a typed host:

```tsx
import "@trampoline-ai/operator-ui/styles.css";
import { OperatorUi } from "@trampoline-ai/operator-ui";
import type { OperatorUiHost } from "@trampoline-ai/operator-ui";

const host: OperatorUiHost = {
  api: operatorApi,
  presentation: {
    rootLabel: "Example project",
    brandImageUrl: "/brand.svg",
    unavailableDescription: "The operator is unavailable.",
    workflowReloadDescription: "Workflow changes are being loaded.",
  },
};

export function WorkflowPage() {
  return <OperatorUi host={host} />;
}
```

Use `GrpcWebOperatorApi` when the host has a compatible gRPC-Web endpoint, or implement the
`OperatorApi` interface for another transport. `WorkflowWorkspace` is also available when the
host supplies its own surrounding navigation and layout.

This package does not run an operator server, create an authentication boundary, or provide
multi-tenant behavior. Those concerns stay with the embedding host.
