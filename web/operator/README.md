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
**Current** shows the workflow definition until a new run arrives, then selects
that run and follows subsequent arrivals, including runs discovered on reconnect.
Existing history is not selected when opening Current. The current
graph and inspector stay visible while another run snapshot loads. The inspected
node stays selected when its ID exists in the destination topology.
Loaded historical runs remain available if their current workflow definition is
removed from the catalog. Completing a run-start request selects the new run only
if workflow/run navigation has not changed in the meantime.
While a run is **Requesting**, its nodes remain visible but dimmed and unavailable
for inspection, alongside a loading indicator. Until the run's own topology arrives,
the graph previews the current workflow definition; it does not represent execution
state. The preview stays visible through the prepared-snapshot refresh, then switches
to the run's topology and enables inspection.
The floating timeline pins **Current definition** above a virtualized run list.
It reveals loaded history in 25-run batches near the bottom, with no twenty-run cap.
The header's icon-only **Expand timeline** action opens expanded history. Selection
highlights Current definition or the selected run; the Current row has no trailing caret.
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

Classifier calls use a compact table with Call, Outputs, Status, and Duration
columns, 25-row pages, and Previous/Next controls matching the expanded timeline.
Invocation numbers and typed compact answers come from paged and live activity
descriptors, without input previews or full-body prefetching.
Calls start at index 1 in ascending order, with every row collapsed. Only explicitly
expanded calls fetch full details. Their eight-entry, 8 MiB cache holds full bodies only.
Evicting a body leaves the list summary intact, and reopening its call reloads
details automatically. Classifier descriptors require `classifier_summary`,
including when the corresponding detail body has expired.

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

### Optional host timeline props

`WorkflowWorkspace` accepts these flat, additive optional props. With all omitted,
Avalanche shows **Current definition**, no trailing action or tabs, and its usual
run history. Hosts supply data and callbacks; operator-ui owns the rendered UI.
`WorkspaceDeployment` and `WorkspaceDeployments` are exported package types.

| Prop                                                             | Default / behavior                                                                                                   |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `currentLabel?: string`                                          | `"Current definition"` (nullish fallback). Delta uses `"Current deployment"`.                                        |
| `currentRowAction?: { label: string; onClick: () => void }`      | No action or caret. When supplied, lowercase underlined link text (underline off on hover); does not select Current.|
| `deployments?: WorkspaceDeployments`                             | Absent: no tabs. Present: **Overview \| Deployments** tabs in both compact and expanded headers.                         |
| `timelineView?: "runs" \| "deployments"`                         | Omitted: internal state starts at `"runs"`. Supplied: controlled by the host. Without `deployments`, only runs show. |
| `onTimelineViewChange?: (view: "runs" \| "deployments") => void` | No callback by default. Called on tab requests in both modes; controlled hosts must update `timelineView`.           |

```tsx
const [timelineView, setTimelineView] = useState<"runs" | "deployments">("runs");

<WorkflowWorkspace
  api={operatorApi}
  workflowId={workflowId}
  currentLabel="Current deployment"
  currentRowAction={{ label: "See all", onClick: () => setTimelineView("deployments") }}
  timelineView={timelineView}
  onTimelineViewChange={setTimelineView}
  deployments={deployments}
/>;
```

The host's `deployments: WorkspaceDeployments` contains:

- `items: WorkspaceDeployment[]`: newest-first, workflow-filtered
  `{ id: string; label: string; createdAt: number; current: boolean }` rows.
  `createdAt` is epoch seconds. `label` is displayed; full `id` is in the title
  and accessible name. Mark at most one item `current` for the
  **current ●** status text (lowercase, green dot after, not a link) in the Status column and `aria-current="true"` on its row.
- `hasMore: boolean`, `loading: boolean`, `loadMore?: () => void`: virtualized
  infinite scrolling requests a page within four row-heights of the bottom.
  It latches each request to avoid duplicate scroll calls until the item count
  changes or the host's loading state changes. The host must set `loading` while
  fetching and clear it after settling. No loader is called while loading,
  after `hasMore` becomes false, or while `error` is set. Omit `loadMore` for a
  fully loaded list.
- `selectedId?: string` (default none) highlights a row.
  `onSelect: (id: string) => void` asks the host to show its definition; it does
  not move the current pointer or select a run.
- `error?: string` (default none) shows an inline error. Loading and empty states
  are rendered by operator-ui, not by the host.
- `setCurrent?: { label?: string; confirmText: (item: WorkspaceDeployment) => string;
onConfirm: (id: string) => Promise<void> }` (default none) adds a non-current-row
  link-style button, labeled **set current** unless overridden (rendered lowercase),
  styled exactly like `currentRowAction`: underlined at rest, underline removed on hover,
  trailing icon, no border/background/padding; keyboard focus has a visible ring.
  It stays visible on every non-current row and opens an inline confirmation
  with the host's wording and Confirm/Cancel buttons. Row actions are disabled
  while confirmation is open; the requested row shows a spinner and `aria-busy`
  throughout confirmation and the request. Confirm closes on resolve and displays
  the rejection's `Error.message` inline on failure.
  Neither the action nor its confirmation selects a deployment. The host updates
  `items[].current` after success.

The pinned Current row and its optional action appear only on **Overview**, not on
**Deployments**. Run status chips and run filters appear only in the expanded
**Overview** view. Changing tabs does not
change run selection or expand/collapse the timeline.

The header shows **Overview | Deployments** tabs when `deployments` is supplied, or a
non-clickable selected **Overview** label in stock mode. The selected label has a 2px
primary-color underline on the header border; inactive tabs are muted. ArrowLeft,
ArrowRight, Home and End focus and request the corresponding tab. Controlled hosts
remain responsible for updating `timelineView`. The visible **Overview** label keeps
its public `timelineView` / `onTimelineViewChange` value `"runs"` unchanged.
Only the Status header and status/action column are right-aligned in both lists;
deployment IDs, created dates and run timestamps stay left-aligned.

The floating timeline's icon-only **Expand timeline** action (also its tooltip and
accessible name) expands it into a wider, full-height timeline panel on the left of
the workspace, independently of node inspection on the right. Above 1000px it
occupies its own column beside the canvas and logs;
below that breakpoint it remains an overlay. It paginates the runs supplied by
the API baseline in 25-run pages, with single-select status count chips and
inclusive local-date filtering. Run-ID search is available
under the initially collapsed **Advanced filters** section. **Collapse timeline**
or Escape restores the floating timeline and returns focus to its trigger.
Filters and run selection survive expansion and collapse.
Expansion and collapse animate the timeline's size and position unless reduced
motion is requested.

Pagination covers loaded history only; it does not retrieve records unavailable
through the host's `OperatorApi`.

This package does not run an operator server, create an authentication boundary, or provide
multi-tenant behavior. Those concerns stay with the embedding host.
