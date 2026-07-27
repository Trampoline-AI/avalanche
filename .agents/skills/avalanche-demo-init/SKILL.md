---
name: avalanche-demo-init
description: >-
  Initialize and verify an Avalanche workflow workspace with editable local
  clones of Avalanche and PredictRLM under .trampoline-ai and the Avalanche
  authoring skill installed at project scope. Use only when someone asks to set
  up, bootstrap, or prepare a workspace; it does not design or create the
  user's workflow.
compatibility: >-
  Requires Git, UV, a Node.js package runner such as npx, network access to
  GitHub, and Python 3.11, 3.12, or 3.13.
metadata:
  author: Trampoline AI
  version: "1.0"
---

# Initialize an Avalanche workflow workspace

Create one standalone UV workspace containing a collection of Avalanche
workflows. The result must work without publishing new Avalanche or PredictRLM
releases.

## Scope

This skill performs workspace setup only. Do not design, propose, scaffold, or
invoke an Avalanche workflow for the user. The bundled `binary_converter` is a
fixed, operator-discoverable example flow, not the user's requested workflow.
Stop after setup and static verification; creating or executing a workflow is a
later, separate request.

## Check for updates

If this skill was installed through the Skills CLI, check for an update before
initializing a workspace:

```bash
npx skills update avalanche-demo-init
```

Replace `npx` with `pnpx`, `bunx`, or the equivalent package runner in use.

## Fixed outcome

The initialized workspace has these properties:

- it is one UV project at `./avalanche-workflows/`;
- its workflows are direct children of `src/`, with no
  `src/avalanche_workflows/` wrapper package;
- it starts with the README's real `binary_converter` agent workflow;
- `.trampoline-ai/avalanche` and `.trampoline-ai/predict-rlm` are ordinary,
  independent Git clones;
- UV resolves both `avalanche-ai[all]` and `predict-rlm` from those editable
  local paths;
- the `avalanche` authoring skill is installed from the local Avalanche clone
  at project scope, never globally;
- the starter contains only `flow.py` and is statically imported and checked
  against the operator CLI without making a model call.

Do not use Git submodules, Git subtree, published-package fallbacks, a separate
`workflows/` directory, or a global skill installation. Do not initialize over
an existing non-empty workspace or overwrite user files.

## Workspace location

Create `avalanche-workflows/` in the agent's current working directory. Users
choose its placement by starting the agent in the desired parent directory. Use
another location only when the user explicitly requests it.

## Choose the starter model backend

Before running the initializer, ask which model backend the user wants for the
starter workflow. Offer these choices in this order:

1. **CodexLM** — writes a DSPy `CodexLM` instance directly into the starter
   flow, using the user's ChatGPT/Codex subscription rather than an OpenAI API
   key. The initializer installs `predict-rlm[codex-lm]`; complete CodexLM
   subscription authentication before a later operator execution.
2. **OpenAI API (default)** — configures `openai/gpt-5.6-terra`. A later
   operator execution requires `OPENAI_API_KEY`.
3. **Other LiteLLM-compatible provider** — ask for the exact LiteLLM model
   string and credential environment variable. The initializer writes that
   model string into the starter flow. Provider support follows LiteLLM's
   supported model and credential configuration.

If the user does not choose, use OpenAI API. Missing credentials MUST NOT block
workspace creation because initialization never executes a model call.

## Initialize with the bundled script

From the desired parent directory, invoke the command matching the selected
backend:

```bash
# CodexLM
python <skill-directory>/scripts/init_project.py --provider codex

# OpenAI API (default)
python <skill-directory>/scripts/init_project.py --provider openai

# Other LiteLLM-compatible provider
python <skill-directory>/scripts/init_project.py \
  --provider other \
  --model <provider/model> \
  --credential-env <PROVIDER_API_KEY>
```

The script creates `./avalanche-workflows`, validates prerequisites, refuses a
non-empty workspace, clones both framework repositories, configures editable
sources, installs the project-scoped `avalanche` skill, writes the real
`binary_converter` workflow and root `AGENTS.md`, synchronizes dependencies,
and verifies every non-model boundary. Do not replace the script with manual
setup steps or substitute PyPI dependencies.

The script is intentionally non-destructive. If setup fails after it creates
the workspace, inspect the reported failure and remove that generated directory
only after confirming it contains no user work; then rerun the script.

## Verification

The script always verifies local editable imports, the project-scoped skill,
the generated guidance, ignored nested checkouts, nested Git branches, static
import of the sole starter `flow.py`, and availability of the `ava operator`
and `ava run` commands. It never executes a workflow or makes a model call
during initialization.

## Handoff

Report the workspace root, sole starter `flow.py` path, configured model
backend, active branch of each nested repository, installed skill location, and
that the operator and run commands were verified. Explain that future workflows
are sibling directories under `src/`, while framework fixes are committed from
the corresponding nested checkout.

Tell the user to start the starter flow's local operator and TUI with:

```bash
uv run ava dev --flows src/binary_converter/
```

Stop there. Do not invoke the installed `avalanche` skill or begin a workflow
outcome interview. On a later user request to create an Avalanche workflow, the
workspace `AGENTS.md` directs the agent to use that installed skill.
