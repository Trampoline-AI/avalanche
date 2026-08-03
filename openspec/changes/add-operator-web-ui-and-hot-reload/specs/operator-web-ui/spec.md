## Purpose

Provide a browser interface for a local operator that gives users live workflow observability and run control while preserving the distinction between current workflow definitions and historical executions.

## ADDED Requirements

### Requirement: Present current workflow state

The web UI SHALL present a workflow view from the current operator catalog, including workflow metadata and its current graph topology. The view SHALL update when the operator publishes a catalog change.

#### Scenario: Workflow topology changes while open
- **WHEN** the selected workflow's current catalog definition changes
- **THEN** the web UI updates that workflow view to the new topology and metadata without requiring a full page reload

#### Scenario: Workflow becomes newly available
- **WHEN** the operator publishes a newly discovered workflow
- **THEN** the web UI makes that workflow available for selection in its current-workflow view

### Requirement: Navigate scanned workflows and runs

The web UI SHALL organize the Explorer by each configured scan target. Each target SHALL contain its discovered workflows, and each workflow SHALL contain its retained runs. Selecting a workflow SHALL open its current workflow view; selecting a run SHALL open that run's historical execution view.

#### Scenario: Multiple scan targets contain workflows
- **WHEN** the operator is configured with multiple file or directory scan targets
- **THEN** the Explorer presents each target as a distinct root and groups workflows beneath their originating target

#### Scenario: Explorer renders scan-target identity
- **WHEN** the operator publishes its configured scan targets
- **THEN** each Explorer root uses the target's normalized path and identifies whether it is a file or directory

#### Scenario: User selects a run
- **WHEN** a user selects a retained run beneath a workflow
- **THEN** the web UI opens the run view without replacing it with the current workflow definition

### Requirement: Inspect the current workflow on a read-only canvas

The current workflow view SHALL present its DAG on a pannable, zoomable, read-only canvas. Agent-node cards SHALL show their declared input and output fields inside the card. Each graph dependency SHALL render as at most one arrow between its source and target cards, regardless of the number of fields supplied across that dependency.

#### Scenario: Agent node has several fields from one parent
- **WHEN** several declared fields of an agent node are supplied by the same upstream node
- **THEN** the canvas renders one arrow between the two node cards and retains the field lists inside the target card

#### Scenario: User inspects an agent declaration
- **WHEN** a user opens an agent node from the current workflow view
- **THEN** the UI presents its instructions in a readable format together with its model, runtime, skill, and tool metadata

### Requirement: Present historical run state

The web UI SHALL present a run view from the selected run's retained topology and execution data. It SHALL not render a run using the current workflow topology when the two differ.

#### Scenario: Historical topology differs from current workflow
- **WHEN** a user opens a run whose recorded topology differs from the current workflow catalog
- **THEN** the run view displays the recorded topology and its node statuses, logs, and available details

#### Scenario: Run node failed
- **WHEN** a run node fails with a retained error message
- **THEN** the run view displays its failed status, elapsed time, and bounded error message

### Requirement: Inspect agent invocation inputs and outputs

The run view SHALL present the retained input and terminal output fields for an agent invocation separately from its trace timeline. It SHALL associate values with their declared signature field names, types, and descriptions when declaration metadata is available.

#### Scenario: Agent invocation has retained inputs
- **WHEN** a user opens a run node whose agent invocation contains retained input values
- **THEN** the run view presents those values by declared input field

#### Scenario: Agent invocation succeeds with outputs
- **WHEN** an agent invocation publishes `run.succeeded` evidence with terminal outputs
- **THEN** the run view presents those values by declared output field

#### Scenario: Agent field contains a PredictRLM file
- **WHEN** a retained agent input or output contains a `predict_rlm.File` with a non-empty host path
- **THEN** its ordinary field value is a structured, typed file value and the run view presents its path using file-specific formatting

#### Scenario: PredictRLM file later changes or disappears
- **WHEN** a displayed host path becomes stale after the run
- **THEN** the web UI retains the reported value without copying, storing, downloading, or treating the browser as authoritative for the file

#### Scenario: Agent value is unavailable
- **WHEN** an input or output cannot be safely represented within the bounded agent-detail contract
- **THEN** the run view identifies that field as unavailable rather than treating an arbitrary string conversion as its value


### Requirement: Inspect complete agent run traces on demand

The run view SHALL make the retained exportable `RunTrace` information inspectable, including run-level status, models, iteration counts, duration, usage and telemetry metadata; every iteration's reasoning, code, outputs, finish metadata and usage; tool calls; predict subcalls; and lifecycle evidence. Inputs and terminal agent outputs SHALL remain separate views.

#### Scenario: User selects a trace turn
- **WHEN** a user selects a trace turn
- **THEN** the reader presents that iteration's retained reasoning, code, truncated and available full output, tool call arguments/results/errors, grouped predict-call inputs/outputs/errors, finish metadata, duration, and usage

#### Scenario: User selects an earlier live turn
- **WHEN** a user selects a completed earlier turn while an agent trace is live
- **THEN** the reader presents that turn and stops automatically following the latest turn

#### Scenario: Agent trace reports an error
- **WHEN** a trace turn records an error
- **THEN** the navigator visibly identifies that turn as failed

#### Scenario: Trace contains many large turns
- **WHEN** a user opens a large trace
- **THEN** the web UI pages and virtualizes lightweight turn summaries, fetches only selected detail bodies, and keeps a bounded detail cache rather than hydrating the complete trace

### Requirement: Provide live operator observability

The web UI SHALL receive ordered operator updates and reconcile its state when its update history is no longer available. It SHALL surface current run status and node state as updates arrive.

#### Scenario: Update replay is unavailable
- **WHEN** the operator requires a client to reset its update stream
- **THEN** the web UI reloads an authoritative catalog and run baseline before resuming live updates

### Requirement: Treat the operator as authoritative

The web UI SHALL derive workflow, run, and detail state from operator responses and ordered updates. It MAY retain ephemeral selection, viewport, inspector, cache, and in-flight action state, but SHALL NOT persist a browser-side run history, artifact store, or authoritative lifecycle state.

#### Scenario: Run creation is accepted
- **WHEN** a user starts a run and receives its run identity
- **THEN** the web UI treats the run as created only when it receives the operator's authoritative run state or reset baseline

#### Scenario: Cancellation is requested
- **WHEN** a user requests cancellation of an active run
- **THEN** the web UI may show the request in flight and reconciles the run status from the operator rather than locally declaring it cancelled

### Requirement: Provide operator run control

The web UI SHALL allow a user to start a selected workflow run and cancel an active run using the same operator behavior exposed to other operator clients. Run input SHALL be an optional schema-blind JSON editor that is closed by default and does not require workflow discovery to publish a `BaseInput` schema.

#### Scenario: User starts a run without opening input
- **WHEN** a user invokes the primary Run action while the JSON editor is closed
- **THEN** the web UI requests a run without a workflow input payload

#### Scenario: User supplies known workflow input
- **WHEN** a user deliberately opens the input editor, enters a JSON object, and invokes Run
- **THEN** the web UI sends that object unchanged as workflow input and relies on authoritative operator validation

#### Scenario: Operator rejects workflow input
- **WHEN** submitted JSON does not satisfy the workflow's `BaseInput`
- **THEN** the web UI displays the operator validation error without creating local run state

### Requirement: Remain local-first by default

The web UI listener SHALL default to loopback-only access. Enabling non-loopback access SHALL require an explicitly trusted and authenticated external boundary.

#### Scenario: Default launch
- **WHEN** a user starts the operator web UI without an explicit listener host
- **THEN** the UI is reachable only through a loopback address

### Requirement: Preserve navigation and controls on narrow viewports

The web UI SHALL retain access to workflow and run navigation, the selected view's
identity, and its primary controls at viewport widths of 375 CSS pixels or greater.
The workflow and run canvases SHALL remain bounded by the visible workspace rather
than forcing primary content beyond the document viewport.

#### Scenario: User opens the operator on a narrow viewport
- **WHEN** the browser viewport is 375 CSS pixels wide
- **THEN** the user can access the Explorer hierarchy, select workflows and retained runs, read the selected view title, and use its primary controls without horizontal document scrolling

#### Scenario: User opens a workflow graph on a narrow viewport
- **WHEN** a current workflow or historical run canvas is displayed at 375 CSS pixels wide
- **THEN** the canvas remains inside the visible workspace and the user can pan and zoom the graph

### Requirement: Expose accessible controls and readable text

The web UI SHALL provide an accessible name for every interactive control and input.
Text and meaningful graphical labels SHALL meet WCAG 2.2 Level AA minimum contrast
requirements in their rendered states.

#### Scenario: User opens the JSON input editor
- **WHEN** the schema-blind JSON input editor is visible
- **THEN** assistive technology identifies it by a descriptive workflow-input name

#### Scenario: Secondary workflow metadata is displayed
- **WHEN** connection, catalog, workflow, run, or node metadata is rendered
- **THEN** its foreground and background colors meet WCAG 2.2 Level AA minimum contrast

### Requirement: Distinguish repeated node invocations

The current-workflow and historical-run canvases SHALL expose a distinct visible and
accessible identity for each node, including when several nodes invoke the same
declared function or agent.

#### Scenario: A workflow invokes the same declaration more than once
- **WHEN** two or more nodes have the same display name
- **THEN** each node card remains distinguishable by a stable invocation identity in both its visible label and accessible name

### Requirement: Bound large-run browser hydration

The web UI SHALL preserve operator pagination instead of draining descriptor pages or
hydrating every retained run snapshot. It SHALL load a run snapshot only for the selected
run, request log and agent-event pages only for the active inspector tab, cancel superseded
requests, and retain bounded descriptor and detail projections.

#### Scenario: User opens a historical run
- **WHEN** a user selects one retained run from a catalog containing many runs
- **THEN** the browser requests one current snapshot for that run without requesting snapshots for the other retained runs

#### Scenario: User opens a node overview
- **WHEN** a user opens a run node and leaves the inspector on Overview
- **THEN** the browser does not request log pages, agent-event pages, or detail bodies

#### Scenario: User inspects a large log or trace history
- **WHEN** the selected node has more descriptors than one page
- **THEN** the active tab requests and renders one bounded page at a time and retrieves older pages only as the user navigates toward them

### Requirement: Keep live browser projections bounded

The web UI SHALL apply operator updates in exact sequence while bounding pending browser
work and retained live descriptor tails. If it cannot preserve the ordered stream within
those bounds, it SHALL discard the ephemeral projection and reconcile from an authoritative
operator baseline rather than dropping arbitrary structural updates.

#### Scenario: Live updates exceed the browser queue bound
- **WHEN** ordered updates arrive faster than the browser can apply its bounded batches
- **THEN** the browser stops the stale stream and reloads authoritative state before resuming

#### Scenario: Live descriptors exceed a retained tail
- **WHEN** a selected run or node publishes more live descriptors than the browser tail retains
- **THEN** the browser records a repair watermark and refreshes authoritative snapshot tokens before paging across the discarded range

### Requirement: Keep inspector rendering responsive

The run inspector SHALL virtualize large descriptor navigators, render only its active tab,
distinguish loading from empty and error states, decode log bodies as text, decode structured
agent-event bodies as JSON, and render nested values through explicit bounded expansion.
Trace following SHALL affect only the Trace tab.

#### Scenario: User changes tabs during hydration
- **WHEN** an earlier tab request completes after the user changes tab, node, or run
- **THEN** the stale result does not replace the current inspector state

#### Scenario: User views plain-text logs
- **WHEN** a selected log body is not JSON
- **THEN** the Logs tab presents its exact decoded text without reporting a JSON parse error

#### Scenario: Retained value contains a large collection
- **WHEN** an input, output, or trace detail contains a large nested collection
- **THEN** the value starts collapsed and renders bounded child groups only after explicit expansion
