---
name: avalanche
description: >-
  Design, build, run, and refactor agentic workflows with Avalanche. Use when a
  user describes an outcome that should become a typed multi-step workflow, or
  when creating an Avalanche flow or belt, choosing deterministic versus
  PredictRLM-backed agent steps, composing DAGs with >> and &, selecting
  embedded/operator/TUI execution, or using Iceberg or Lance persistence.
compatibility: >-
  Requires Python 3.11+ and avalanche-ai; agent steps require the agent extra.
  Includes a vendored copy of PredictRLM's RLM-design skill as a reference;
  check its upstream source for updates when access is available.
metadata:
  author: Trampoline AI
  version: "1.0"
---

# Design and build Avalanche agentic workflows

> Before using this skill, check for an update with
> `npx skills update avalanche` when it was installed through the Skills CLI.
> Replace `npx` with `pnpx`, `bunx`, or the equivalent package runner in use.

Avalanche turns a desired outcome into a typed, observable agentic workflow. A
workflow combines deterministic Python nodes with PredictRLM-backed agent nodes
inside one DAG, then runs it locally or through the operator and TUI. The main
design task is deciding where adaptive agent work belongs, what each stage
promises, and how data and artifacts move between stages. Decorators and arrow
syntax come after that design.

## Route to the right reference

Load only what the task needs:

- Designing and implementing one PredictRLM-backed Avalanche step:
  [agent-steps.md](references/agent-steps.md). It loads the vendored original
  PredictRLM skill for the single-step design process, then covers the Avalanche
  integration surface.
- First implementation example: [quick-start.md](references/quick-start.md).
- File layout and maintainability: [project-layout.md](references/project-layout.md).
- Iceberg and Lance persistence: [storage.md](references/storage.md).

This skill designs the complete agentic workflow: stage boundaries,
deterministic work, agent responsibilities, typed handoffs, topology,
persistence, execution, and end-to-end verification. The agent-step reference
uses the original PredictRLM skill to design each individual adaptive agent
before implementing its Avalanche wrapper.

## Design an agentic workflow

Design has two user-alignment gates: agree on the outcome, then approve a
concrete workflow plan. Do not implement, scaffold, or edit workflow code until
the user explicitly approves the presented plan.

### Deliver the workflow, not the requested outcome

The deliverable is a working Avalanche workflow that can achieve the user's
outcome when the user runs it. You are building that workflow—not performing
the workflow's domain work for the user now.

Do not independently research, analyze, transform, publish, notify, or otherwise
act on the user's real inputs to produce the requested business result outside
the constructed workflow. Use supplied materials to understand requirements and
to define contracts. Exercise the requested behavior only by running the
implemented Avalanche workflow for verification, using safe test inputs and
destinations unless the user explicitly authorizes real side effects.

This boundary remains after plan approval: implementation produces the workflow
code, configuration, and verification evidence, not a one-off substitute for
the workflow's eventual output.

### Step 1: Interview the user and align on the outcome

Read any description or materials the user already supplied, then ask focused
questions. At minimum, establish:

- **Goal:** What do you want the workflow to accomplish, and for whom?
- **Success:** What does a good result look like? What will you inspect or
  evaluate first?
- **Inputs:** What information goes in? Identify its source, format, volume,
  authority, and whether it is documents, files, data, code, APIs, feedback, or
  persistent state.
- **Outputs:** What do you expect to receive? Identify the required structured
  data, files, reports, edits, notifications, destination, and presentation
  format.
- **Boundaries:** What may the workflow change, what must remain read-only, and
  which decisions require human approval?
- **Operating constraints:** Is this one run or a recurring process? Which
  quality, latency, cost, privacy, compliance, or environment constraints are
  material?

Ask follow-up questions when an answer changes the outcome or boundary. Do not
substitute architecture questions such as “How many agents?” for outcome
questions. The user supplies domain intent; the agent owns the later workflow
decomposition and implementation decisions.

Before drafting a workflow plan, summarize the agreement in this form and have
the user align on it:

> Given **[authoritative inputs]**, the workflow will **[accomplish the goal]**
> and produce **[specific outputs and destination]**. It succeeds when
> **[observable criteria]**, while respecting **[side effects, approval points,
> and constraints]**.

Separate the requested outcome from a proposed implementation. “Review these
submissions and publish an approved package” is an outcome; “use five agents” is
an architecture claim that still needs justification.

### Step 2: Decompose the work into meaningful stages

Start with the smallest stage graph that can deliver the outcome. Each stage
must have one stable responsibility, an observable result, and a reason to exist
as an independent node.

For an agent stage, one stable responsibility means **one logical unit of
work**: one specific task with one cohesive result. That result may contain
multiple related fields or files when they jointly complete the same task.
Completing the task may require an internal sequence such as survey, extract,
compare, and validate; those are steps in the RLM's strategy, not separate
workflow responsibilities. Do not assign one agent a list of independently
useful tasks or deliverables.

Define each logical agent task with:

- one imperative responsibility;
- authoritative inputs and inherited decisions it must preserve;
- one cohesive typed result;
- explicit non-goals that belong to earlier or later stages;
- a completion condition that can be validated at its boundary.

Split proposed agent work into one agent step per logical task. A logical task
has one purpose, produces one cohesive result, and can be judged complete on its
own. These are signals that the work contains distinct logical tasks:

- outputs could be accepted, retried, reused, or changed independently;
- one result is consumed separately by later stages;
- portions can run independently or in parallel;
- portions require genuinely different responsibilities or validation;
- each portion creates its own durable or reviewable artifact.

Different tools, permissions, capabilities, or budgets may support a boundary,
but they do not determine the number of agent steps. Do not merge separate
logical tasks merely because they share context or capabilities. Merge
activities only when they jointly complete one task and the intermediate work
has no independent purpose. Separate deterministic work into `@ava.step` nodes
when it forms an expressible, testable boundary. Do not turn every procedural
action inside one task into a node.

### Step 3: Assign the right execution type

For each stage, choose:

- `@ava.source` to ingest or construct the first runtime value;
- `@ava.step` for deterministic parsing, normalization, calculation, lookup,
  validation, conversion, routing, or artifact assembly;
- `@ava.agent_step` when the stage requires adaptive exploration, evidence
  gathering, tool choice, judgment, or synthesis;
- `@ava.dest` for the final publish, export, notification, or external write.

Prefer deterministic code whenever the algorithm is known. The number of agent
steps follows the number of logical agentic tasks: use one agent step for each
task. Do not merge distinct tasks because they share context or capabilities,
and do not split one task into a chain of prompt fragments. Artifacts,
capabilities, budgets, and validation boundaries can expose task boundaries,
but the logical tasks decide the decomposition.

### Step 4: Design typed stage contracts

Define the workflow's `ava.BaseInput`, final result, and every meaningful
inter-stage payload before writing node bodies. For each stage record:

- input model and source;
- source authority and prior-stage decisions the node must not rediscover or
  change;
- output model or artifact;
- invariants the stage establishes;
- errors or incomplete states the next stage must handle;
- explicit non-goals owned by adjacent stages;
- whether large content should travel as a file/table reference instead of an
  injected string.

Contracts should expose facts and artifacts needed by downstream stages, not
private reasoning. A stage boundary is justified only when its output can be
named, validated, reused, persisted, or inspected.

### Step 5: Design each RLM agent

For every proposed agent stage, use
[agent-steps.md](references/agent-steps.md), which invokes the bundled original
PredictRLM skill for Steps 1–6:

1. validate that this stage is a good RLM fit;
2. define its inputs;
3. define its outputs;
4. research sandbox and host-side feasibility;
5. select reusable Skills, host tools, and any extension boundary;
6. write the complete strategy in the signature docstring.

The signature docstring is that agent's instruction. Do not move
agent-specific instructions into a one-off Skill. A Skill represents reusable
knowledge or capability shared by multiple agents.

### Step 6: Draw the dependency graph

Connect stages from data dependencies, not from an imagined conversation among
agents.

- Run stages in parallel only when neither consumes the other's result.
- Fan out when one validated result feeds independent work.
- Fan in through a typed stage that names and validates the combined result.
- Keep explicit references to earlier outputs when a downstream stage needs
  more than its immediate predecessor.
- Avoid serial agent chains whose intermediate outputs merely restate the same
  context.

The graph should make critical inputs, concurrency, reusable outputs, and the
terminal result visible without reading node bodies.

### Step 7: Choose persistence and execution deliberately

Use in-memory typed values for ordinary within-run handoffs. Add Iceberg or
Lance when artifacts must survive runs, be queried or audited, feed an
incremental backlog, or be consumed outside the immediate process. Persistence
is an architectural requirement, not a default stage.

Choose the execution surface from the caller:

- embedded execution for application code that starts a run and consumes its
  `RunHandle`;
- operator/CLI execution for discovered flows run through the control plane;
- the TUI when users need to launch, inspect, monitor, or control operator runs.

### Step 8: Check feasibility and define proof

Before implementation, verify:

- every stage has a necessary responsibility and typed boundary;
- every agent stage has completed the RLM feasibility and capability design;
- independent work is parallel and required dependencies are explicit;
- host tools, network access, packages, files, and persistence are available;
- the final output can be checked against concrete acceptance criteria;
- one representative end-to-end scenario exercises the actual execution
  surface and terminal result.

### Step 9: Present the workflow plan and obtain approval

Turn the completed design into a user-facing proposal using the required
workflow-plan format below. The plan is a proposal, not an implementation
instruction: explain the selected stages, their dependencies, node types, and
capabilities so the user can evaluate the design rather than infer it from
code.

End by asking for explicit approval to implement the proposed plan. Do not
create project files, write node bodies, run scaffolding, or begin any other
implementation activity until the user affirmatively approves it. A question,
comment, or requested change is not approval; revise the plan and present it
again. Once approved, implement the approved plan and surface any later change
that would materially alter its goal, boundary, or topology.

## Examples: from a goal to agent steps

Use these examples to calibrate logical task size. The numbered items are agent
steps; deterministic staging, validation, aggregation, and publication are
called out separately.

### First-pass RFP response package

**Goal:** Turn an issuer's RFP package and available bidder evidence into a
reviewable first-pass response package with analysis, proposal artifacts,
completed returnables where safe, and a customer handoff.

Agent steps:

1. **Audit the RFP package:** establish requirements, deliverables, dates, file
   roles, and returnable routing; return the package audit and control sheet.
2. **Build the vendor evidence profile:** map bidder facts, examples, and gaps
   to the audited requirement IDs.
3. **Plan the submission:** decide safe writes, intentional non-writes,
   unsupported fields, missing inputs, questions, assumptions, and next actions.
4. **Draft proposal content:** write substantive proposal sections and a
   requirement-to-response traceability map; do not render final files.
5. Run three independent rendering tasks in parallel:
   - **Render the internal briefing** from the structured audit report.
   - **Render the proposal document** from the approved content draft.
   - **Render the compliance matrix** from audit, evidence, plan, and
     traceability.
6. Run three independent returnable-filling tasks in parallel:
   - **Fill PDF returnables** by executing the typed fill plans.
   - **Fill spreadsheet returnables** by executing the typed fill plans.
   - **Fill other document returnables** by executing only safe, precise edits.
7. **Compose the customer handoff:** explain the generated package, remaining
   blockers, required review, and next actions without re-deciding prior work.

Deterministic nodes prepare inputs, validate agent outputs, combine parallel
artifacts, and save the final package. Inventorying, extraction, and file
routing are procedures inside the package-audit task; proposal drafting and
document rendering are separate tasks because they produce independently
reviewable results with different completion conditions.

### Persistent project revision loop

**Goal:** Incorporate new information and operator feedback into a persistent
RFP project safely, while preserving an auditable authorization boundary and
publishing only validated revisions.

Agent steps:

1. **Reconcile the project:** inspect the current artifacts and intake and
   return a complete factual account of supported facts, discrepancies, and
   gaps. This step is read-only and neither proposes nor performs edits.
2. **Plan the revision:** turn the reconciliation and intake into the exact,
   reviewable authorization for this revision. This step specifies changes but
   does not touch project files.
3. **Apply the revision:** execute only the authorized changes, verify each
   edit, and report applied, partial, or blocked operations. This step does not
   reinterpret business meaning or broaden the plan.

Deterministic nodes seal the staged baseline, validate the plan and resulting
mutations, enforce structural invariants, and commit atomically. Reconciliation,
authorization, and execution remain separate agent tasks even though they work
on the same project and share context.

### Meeting transcript cleanup

**Goal:** Turn messy or overlapping captures of meetings into accurate,
readable transcripts with grounded decisions, action items, and unresolved
issues.

Agent steps:

1. **Reconcile and clean the meeting transcripts:** group alternate captures,
   choose the strongest timeline, repair gaps and speaker labels from supporting
   sources, preserve uncertainty, and produce readable transcripts plus
   decisions and actions.

Deterministic nodes discover supported input files and publish the returned
models as JSON and Markdown. Surveying sources, grouping captures, repairing
gaps, and extracting actions remain one agent step because they jointly
complete the single transcript-reconciliation task.

## What a good Avalanche workflow looks like

A good workflow is:

- **Outcome-oriented:** its terminal artifact directly satisfies the user's goal.
- **Minimal:** every node adds a real execution or validation boundary.
- **Typed:** node contracts make data flow and failure states inspectable.
- **Deterministic where possible:** LMs do not perform known calculations or
  mechanical transforms.
- **Coherently agentic:** each RLM owns one logical task and one cohesive result.
  Its strategy may be multi-step, but its responsibility is not a list of
  independently useful jobs or one turn of a scripted conversation.
- **Explicitly parallel:** independent branches are visible in the DAG.
- **Capability-bounded:** each agent receives only the Skills and tools it needs.
- **Observable:** intermediate outputs are meaningful enough to inspect and
  validate.
- **Durable only when needed:** storage exists for a stated cross-run or external
  consumption requirement.
- **Readable from `flow.py`:** the file reveals the stages and topology without
  hiding business logic in helpers or the workflow body.

Reject designs built from “one agent per verb,” one agent assigned a list of
independent deliverables, agents that only forward or reformat another agent's
prose, untyped prompt blobs, duplicated agent-specific Skills, unnecessary
persistence, or a giant agent with broad host access and no validated output
boundary.

## Workflow plan output

After completing the design analysis but before any implementation, present a
workflow plan with every section below. Scale the detail to the workflow, but
do not omit a section because the design appears simple.

1. **Outcome agreement:** the aligned outcome statement, success criteria,
   authoritative inputs, required outputs and destination, side effects,
   constraints, and human approval points.
2. **Stage plan:** a table with stage name, node type, responsibility,
   authoritative typed input and source, typed output, required capabilities
   (Skills, tools, permissions, or packages), and why this is a separate
   boundary. State why each stage is deterministic or agentic; list explicit
   non-goals where they prevent responsibility overlap.
3. **Mermaid DAG:** render the proposed data-flow graph as a `mermaid` diagram.
   Every node label must identify its stage name, node type, brief
   responsibility, typed input, and typed output. Show data dependencies,
   parallel branches, fan-in, earlier-stage references, and the terminal
   destination. Use a legend or Mermaid classes so `@ava.source`, `@ava.step`,
   `@ava.agent_step`, and `@ava.dest` remain distinguishable.
4. **Contracts:** complete Pydantic contract sketches for the workflow input,
   each meaningful handoff, and final result, including invariants and
   incomplete/error states at boundaries.
5. **Agent designs:** an RLM Steps 1–6 design for every `@ava.agent_step`,
   including its bounded responsibility and completion condition.
6. **Tools, Skills, packages, and integrations:** inventory every required
   reusable Skill, host tool, Python package, and external integration. For
   each, state its purpose, consuming stage or stages, and whether it already
   exists, is installed as a dependency, or must be developed. Name required
   packages and why they are needed. For a third-party API, specify the
   service, operation, authentication/configuration boundary, client package or
   protocol, typed tool inputs/outputs, and the adapter or tool that must be
   built. Assign only the minimum needed capabilities to each agent stage and
   surface missing prerequisites rather than hiding them in a generic prompt.
7. **Operational decisions:** persistence and execution-surface choices, with
   their rationale.
8. **Implementation and proof:** package/file layout plus a representative
   end-to-end verification scenario and the local operator/web-UI handoff. Name
   the required `runtime` dependency, narrow flow file or clean flow directory,
   operator launch command, expected UI endpoint, and browser check.

End the proposal with a direct request for approval to implement it. Do not
create or modify workflow implementation files, generate scaffolding, or begin
implementation until the user explicitly approves the plan. If the user asks
for changes, update and re-present the complete plan for approval.

## Build the designed workflow

1. Define workflow inputs and every meaningful payload/result as Pydantic models.
2. Choose the package layout before writing `flow.py`.
3. Implement deterministic `@ava.source`, `@ava.step`, and `@ava.dest` nodes.
4. For each agent step, follow the bundled RLM reference's design workflow,
   then define its signature and `@ava.agent_step` body.
5. Declare the DAG at the bottom of `flow.py` as one parenthesized `>>` / `&`
   expression, binding reusable `NodeFuture` values inline with `:=`.
6. For table-backed flows, define and push the Iceberg or Lance namespace, then
   connect table and stream providers to the DAG.

## Launch the local operator and hand off the web UI

After an approved workflow implementation and its focused verification complete,
launch the new flow through the local operator with the browser UI enabled:

```bash
uv run ava operator --flows <flow-file-or-clean-flow-directory> --web
```

Use a specific flow file or a clean flow-only directory; never use `--flows .`.
Run the operator as a long-lived process, wait for it to report
`Avalanche web UI: <endpoint>`, then open that exact endpoint in a browser and
confirm the new workflow appears in the catalog. The default local endpoint is
`http://127.0.0.1:7435`, but report the endpoint actually printed by the
operator.

In the final handoff, give the user the exact URL and explicitly tell them to
open it in their browser. State that it is a local-development loopback service
without built-in authentication. This operator launch and browser check are
required even when the workflow's intended execution surface is embedded;
they demonstrate that the new flow is discoverable and controllable through
Avalanche's operator UI.

## Non-negotiable conventions

- Use Pydantic `BaseModel` classes as the source of truth for workflow data.
  Use `ava.BaseInput` for the runtime input model. Do not pass unstructured
  dictionaries between nodes when a model can express the contract.
- Restrict `flow.py` to imports, decorated node definitions, and workflow
  declarations. Workflow declarations form the final section of the file;
  nothing follows them.
- Put every undecorated helper function in `util.py`, including private,
  single-use, mapping, validation, formatting, conversion, and filesystem
  helpers.
- Define Pydantic models in `schema.py`; keep reusable signature classes, config
  loading, namespace construction, CLI entry points, and execution code out of
  `flow.py`.
- For a small agent contract, construct the inline signature directly inside
  `@ava.agent_step(...)`. Reusable or substantial signatures get one directory
  per agent, with at least `signature.py` and `schema.py`.
- The docstring on an `ava.Signature` class is the agent's instruction. Put all
  instructions specific to that agent step in this docstring.
- A Skill is reusable knowledge or a reusable capability shared by multiple
  agent steps. Never create a Skill solely to carry instructions for one agent
  step; keep those instructions in its signature docstring. Pass reusable Skills
  through `skills=` on each agent step that needs them.
- `agent` is framework-injected, keyword-only, annotated `ava.Agent`, and never
  passed at DAG call sites.
- `await agent(...)` returns the raw DSPy prediction. The body must select,
  validate, compose, and return or persist the intended output explicitly.
- A workflow body defines edges only. No runtime loops, data-dependent branches,
  file/network I/O, or transformations there.
- Always parenthesize parallel groups: `a() >> (b() & c()) >> d()`.
- Bind a node future with `:=` when another node must reference it explicitly,
  such as a fan-in or a dependency on an earlier stage:
  `(s0 := prepare()) >> (s1 := analyze(s0)) >> publish(s0, s1)`.

## Choosing node types

- `@ava.source`: ingest or construct the first runtime value.
- `@ava.step`: deterministic transformation.
- `@ava.agent_step` / `@ava.agent.step`: wrapper around the PredictRLM runtime
  with an injected callable `ava.Agent`.
- `@ava.dest`: publish, export, or summarize final results.

Functions may be synchronous or asynchronous. Calling a decorated node inside a
workflow returns a deferred `NodeFuture`, not its runtime value. Passing that
future to another node creates the dependency.

## DAG notation

### Simple one-to-one flow

Start with standard arrows when each node's output maps positionally to the next
node's input:

```python
@ava.workflow
def document_flow():
    return ingest_documents() >> analyze_documents() >> publish_report()
```

For signatures shaped like:

```text
ingest_documents() -> DocumentBatch
analyze_documents(batch: DocumentBatch) -> DocumentAnalysis
publish_report(analysis: DocumentAnalysis) -> PublishedReport
```

the calls to `analyze_documents()` and `publish_report()` need no explicit
arguments. `>>` records the dependency and passes the immediate upstream result
into the next available positional or positional-or-keyword data parameter.
Framework-injected runtime parameters are skipped.

Parallel results also bind positionally in branch order:

```python
@ava.workflow
def comparison_flow():
    return (load_primary() & load_secondary()) >> compare_batches()
```

This maps to `compare_batches(primary, secondary)` when its parameter order
matches the two branch results. Do not assign intermediate futures in these
positionally aligned flows.

### Complex flow with explicit dependencies

Bind node futures inline when a later node needs outputs that positional chaining
cannot express:

```python
@ava.workflow(input=ProposalInput)
def proposal_flow():
    (
        (s0 := prepare_inputs())
        >> (s1 := audit_package(s0))
        >> (s2 := build_plan(s0, s1))
        >> (
            (s3_briefing := draft_briefing(s1, s2))
            & (s3_proposal := draft_proposal(s0, s1, s2))
            & (s3_matrix := build_compliance_matrix(s1, s2))
        )
        >> (
            s3 := combine_drafts(
                s3_briefing,
                s3_proposal,
                s3_matrix,
            )
        )
        >> publish_package(s0, s1, s2, s3)
    )
```

Use `:=` and explicit node arguments when:

- a node consumes an output from an earlier, non-immediate stage;
- a fan-in needs a subset, different order, or combination of prior outputs;
- the same output feeds multiple later nodes;
- a data argument must be supplied by keyword rather than position;
- the downstream node call already contains explicit workflow arguments.

In this form, `:=` names each reused `NodeFuture`, explicit arguments carry data,
`>>` shows stage ordering, and parenthesized `&` groups show parallel fan-out and
fan-in. Return a `NodeFuture` when an embedded caller needs the terminal value
through the `RunHandle`; a publishing or persisted flow may leave the graph as
the workflow body's expression statement.

## Completion checks

- User and agent aligned on goal, authoritative inputs, expected outputs,
  success criteria, boundaries, and approvals before design.
- Every inter-node data shape is typed with a Pydantic model.
- `flow.py` contains only the allowed declarations, has no helper definitions,
  and ends with its workflow declarations.
- Every agent input/output field matches the keyword arguments used in
  `await agent(...)` and the prediction fields read afterward.
- Every signature class has an instruction-bearing docstring, and every custom
  Skill represents knowledge or capability reused across agent steps.
- Every tool has a stable unique function name, typed arguments, a precise
  docstring, and a serializable return value.
- Embedded execution reaches a real terminal result.
- The completed workflow is discovered by a local operator launched with
  `--web`, and the reported web UI endpoint is opened in a browser.
- The user receives that exact local web UI URL and an explicit instruction to
  open it in their browser.
- Operator discovery, CLI input, and TUI connection are exercised when changed.
- Iceberg or Lance namespaces are pushed and append/stream behavior is exercised
  when persistence is used.
