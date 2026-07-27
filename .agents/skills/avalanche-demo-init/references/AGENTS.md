# Avalanche workflow workshop

## Purpose

This repository is one UV workspace for a collection of Avalanche workflows.
It was created by Avalanche's `avalanche-demo-init` skill. The starter
`binary_converter` is a real agentic workflow copied from the Avalanche README
and statically checked during setup. Whenever a user asks to create an Avalanche
workflow, MUST invoke the installed `avalanche` skill before implementing it.

```text
src/
├── binary_converter/       # starter flow
│   └── flow.py
├── research_assistant/     # future workflow
└── document_reviewer/      # future workflow
```

Each direct child of `src/` is one workflow. Do not add a wrapper package such
as `src/avalanche_workflows/`, and do not create a separate `pyproject.toml`,
virtual environment, or framework checkout for each workflow.

## Starter flow

`src/binary_converter/flow.py` configures **__STARTER_PROVIDER__** with
`__STARTER_MODEL__`. It generates a random binary value and asks the configured
model to convert it to decimal when the operator executes the flow.

The workspace is created whether or not __STARTER_CREDENTIAL__ is ready. Before
operator execution, authenticate the selected backend or configure its
credential. Do not change providers implicitly or reuse credentials from a
different provider.

## Execution boundary

Flows execute through Avalanche's operator. This workspace contains flow
declarations only. From the workspace root, start the starter flow's local
operator and TUI with:

```bash
uv run ava dev --flows src/binary_converter/
```

To invoke a flow from the CLI instead, start an operator:

```bash
uv run ava operator --flows src/binary_converter/ --port 7433
```

Then, in another terminal:

```bash
uv run ava run binary_converter --connect localhost:7433
```

## Workflow boundaries

- Put each workflow's decorated nodes and workflow declarations in its
  `flow.py`; declarations are the final section of that file.
- Keep workflow builders edge-only. Runtime work belongs in nodes.
- Add each new workflow as a sibling under `src/`. Each starter workflow needs
  only `flow.py`; add `schema.py` or `util.py` only when its actual contract or
  helpers require them.

## Editable framework checkouts

The workspace intentionally uses local editable checkouts:

```text
.trampoline-ai/
├── avalanche/       # Avalanche workflow runtime and authoring integration
└── predict-rlm/     # PredictRLM agent runtime used by Avalanche agent steps
```

The outer workspace ignores `.trampoline-ai/`, but each child directory is an
ordinary independent Git repository. The checkouts are not submodules. Their
files are editable dependencies through `pyproject.toml`, so a change in either
checkout is used by the next workspace Python invocation without publishing a
package release.

## Framework contribution policy

When a problem belongs in the local Avalanche or PredictRLM checkout, classify
it before changing framework code.

### Bugs: direct fix and pull request

A bug is reproducible behavior that violates an existing contract, documented
behavior, or established expectation. Work on the relevant local checkout:

1. Identify whether the behavior belongs to Avalanche or PredictRLM.
2. Reproduce it with a focused test or minimal command in that repository.
3. Make the smallest correct fix and add or update behavior-level regression
   coverage there.
4. Run the focused verification in that checkout, plus any directly relevant
   lint or test command.
5. Commit from the affected nested repository, not from this outer workspace.
6. Open a pull request against the corresponding upstream repository.

A bug PR does not require a prior issue, but its description MUST name the
reproduction, violated behavior, root cause, fix, and exact verification.

### Missing features: issue first

A missing capability, new API, changed behavior, or feature a user asks to add
is a feature request, not a bug. Do not open a feature PR by default. File an
upstream issue with the `feature request` label that explains the user need,
proposed behavior, alternatives or constraints, and acceptance criteria.

Only create a feature PR when the feature request has a linked upstream issue
and the user explicitly asks to proceed with implementation. The PR MUST link
to that issue and state the issue number in its description. Do not disguise a
feature as a bug to bypass this policy.

### Remote ownership

Use a branch in the nested checkout. If the upstream remote is not writable,
create or use a fork as `origin`, retain the official repository as `upstream`,
push the branch to the fork, and open the PR to `upstream/main`. Do not wait for
a framework release before continuing to test the workspace: the editable
checkout already contains the fix.

When a bug cannot be fixed during the workshop, file an upstream issue with a
minimal reproduction, expected and observed behavior, environment details, and
the affected commit SHA. Link that issue from the workspace's own notes or pull
request as applicable.

## Git ownership

- Commit workspace and workflow changes from this repository.
- Commit Avalanche changes from `.trampoline-ai/avalanche`.
- Commit PredictRLM changes from `.trampoline-ai/predict-rlm`.

Never mix those histories or commit nested-repository files into the outer
workspace. Before reporting a framework fix as complete, verify its Git status
and branch inside the affected checkout.
