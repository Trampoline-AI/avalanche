# @trampoline-ai/operator-ui

Embeddable React UI for inspecting and controlling Avalanche workflows. The package provides the
workflow graph, run list, run controls, logs, node details, and agent details; the embedding host
owns navigation, the operator connection, and presentation.

This package releases with Avalanche from the same commit and release tag. Use the
version matching your operator: stable versions are identical, while Avalanche
`0.4.0rc1` corresponds to operator UI `0.4.0-rc1` (`a`/`b` map to `alpha`/`beta`).
Stable releases use the npm `latest` tag; prereleases use `next`.

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

The UI bundles the same Nacelle Regular and SemiBold fonts as Delta Console;
code and filename references remain monospace.

`OperatorUi` uses a compact workflow sidebar with padded, square-cornered rows,
8px gaps between workflows, and straight tree connectors reaching the scan-target
icon. Selected labels use Delta's brand blue and retain it on hover. A catalog
with exactly one scan target groups every workflow directly under that target's
file or directory name, with the full path available on hover.
With zero or multiple targets, workflows remain in a
flat list rather than implying an unavailable per-target mapping. Source filename
references remain visible, and long names truncate within the sidebar. The target
is a label, not a project view. The sidebar has no dashboard, profile card, or
Navigator/Explorer heading; its pin/hover controls and reload diagnostics remain.

Both hosts share one workspace with an optional selected run. With no run selected,
nodes expose their available definitions, without execution badges or logs. Selecting
a run uses its immutable topology, status, durations, and collapsible logs. Selecting
the newest run enables following newly created runs; selecting an older run pins it.
**Current** stops following, including across reconnects. The current
graph and inspector stay visible while another run snapshot loads. The inspected
node stays selected when its ID exists in the destination topology.
Loaded historical runs remain available if their current workflow definition is
removed from the catalog. Completing a run-start request selects the new run only
if workflow/run navigation has not changed in the meantime.
The floating **Timeline** shows Current followed by at most the 20 newest runs.
Current scrolls with the run entries rather than staying pinned. When 20 runs are
shown, a final **View all** row opens the expanded history browser; the header
action remains available at all times. Selection highlights Current or the selected run.
Both floating and expanded timelines use a trackless scroll handle that stays faintly
visible whenever the list can scroll, and reaches full opacity while hovering over
the timeline or dragging the handle. Rows extend beneath it with text padded clear.

The inspector has separate current-state and run modes. Its header identifies
`Agent · Current state` or `Step · Current state` in workflow view, and includes
the run ID in run view. Current agent definitions have no tabs: they show
instructions, input/output schemas, configured models, skills, tools, and runtime
configuration. Consistent section headings, underline separators, and spacing
establish hierarchy without boxed groups; inputs/outputs and model roles use quieter
subheadings. Current regular steps show source code.

Run agent inspectors expose only **Trace** and **Run I/O** tabs. Trace occupies the
full panel with turn content. Run I/O pairs each historical field definition
directly with its retained invocation value; missing schemas and values remain
explicit instead of falling back to current metadata. Run headers retain execution
status, duration, model usage, and iteration details. Selecting a regular step in
run view highlights the node and filters its logs without opening a sidebar.
Regular steps still open their source code in Current view.
Run I/O uses the same section headings and separators as the current definition,
with unboxed fields and subtly shaded, padded retained-value explorers.

Open trace details take priority over background summary loading within the
eight-entry, 8 MiB detail cache. If open turns alone exceed that budget, an evicted
section offers **Reload step detail** without discarding its reasoning summary.
Failed requests expose explicit retry actions; oversized details retain their
summary but cannot be loaded beyond the browser limit.

Run snapshots retain the selected node while its ID exists in the destination
topology. The canvas notice reads **Viewing a run snapshot** and states that the
view does not represent the workflow's current state.

Skill titles open a dedicated, wider Markdown popup that renders the complete
document with scrolling, without a **Show more** control. Non-empty **Packages** and
**Modules** sections follow the instructions, displaying declared dependency strings
and module names. Closing it returns focus to the selected skill without expanding
the sidebar. Tools expand inline to show
their full Python source, including docstrings, in a read-only code viewer that
fits short functions and scrolls internally once it reaches its height limit.
Tools without inspectable source show an explicit unavailable message.
Runtime configuration remains collapsible.

Authored Markdown in instructions and expanded resources has scoped heading, list,
paragraph, link, quote, and code typography. These styles do not affect inspector
controls or host content. Active HTML and remote images remain excluded.

Full instructions, current field schemas, configured models, resources, and code
come from the available workflow definition, not the historical run. Hosts can set
`definitionLabel` on `WorkflowWorkspace`, or on `OperatorUi`'s presentation, to
identify missing-definition messages.

For route-controlled embedding, pass
`navigation={{ selectedRunId, onSelectRun }}` to `WorkflowWorkspace`.
`onSelectRun(undefined)` clears the run; changing routes within a workflow should
retain the same component instance. Without `navigation`, selection is managed
locally and `onSelectedRunChange` can observe it.

Pass `bottomRightPanel` to `WorkflowWorkspace` to replace the graph's floating
actions with host controls. Omit it to retain built-in controls, or pass `null`
to hide the panel. The panel stays inside the graph when the inspector opens.

The floating timeline's **Expand timeline** icon expands it into a wider, full-height
**Timeline** panel on the left of the workspace, independently of node inspection on
the right. It paginates the runs supplied by the API baseline in 25-run pages, with a
status dropdown and inclusive local-date filtering. Run-ID search is available
under the initially collapsed **Advanced filters** section. **Collapse timeline**
or Escape restores the floating timeline and returns focus to its trigger.
Filters and run selection survive expansion and collapse.
Expansion and collapse animate the timeline's size and position unless reduced
motion is requested.

Pagination covers loaded history only; it does not retrieve records unavailable
through the host's `OperatorApi`.

This package does not run an operator server, create an authentication boundary, or provide
multi-tenant behavior. Those concerns stay with the embedding host.
