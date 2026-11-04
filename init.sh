#!/usr/bin/env bash
# Create a verified Avalanche demo workspace in the current empty directory.
set -Eeuo pipefail

readonly AVALANCHE_REPOSITORY="https://github.com/Trampoline-AI/avalanche.git"
readonly PREDICT_RLM_REPOSITORY="https://github.com/Trampoline-AI/predict-rlm.git"
readonly PROJECT_NAME="avalanche-workspace"
readonly STARTER_WORKFLOW_NAME="start_flow"

workspace_root="$(pwd -P)"
staging_root=""
readonly SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly AVALANCHE_SKILL_SOURCE="$SCRIPT_DIRECTORY/skills/avalanche"
use_editable_dependencies=false
fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [[ -n "$staging_root" && -d "$staging_root" ]]; then
    rm -rf "$staging_root"
  fi
}
trap cleanup EXIT

require_command() {
  local command=$1
  command -v "$command" >/dev/null 2>&1 || fail "Missing required command: $command"
}

require_empty_directory() {
  local entries=()
  shopt -s dotglob nullglob
  entries=("$workspace_root"/*)
  shopt -u dotglob nullglob
  ((${#entries[@]} == 0)) || fail "Current directory is not empty: $workspace_root
Create and enter an empty directory, then rerun init.sh."
}

parse_args() {
  while (($#)); do
    case "$1" in
    --editable-deps) use_editable_dependencies=true ;;
    -h | --help)
      printf 'Usage: init.sh [--editable-deps]\n'
      printf '  --editable-deps  clone Avalanche and PredictRLM as editable dependencies\n'
      exit 0
      ;;
    *) fail "Unknown option: $1" ;;
    esac
    shift
  done
}

check_prerequisites() {
  require_empty_directory
  require_command git
  require_command uv

  if [[ "$use_editable_dependencies" == true ]]; then
    printf 'Checking GitHub access for editable dependencies...\n'
    git ls-remote --exit-code "$AVALANCHE_REPOSITORY" HEAD >/dev/null 2>&1 ||
      fail "Cannot reach the Avalanche repository on GitHub. Check network access."
    git ls-remote --exit-code "$PREDICT_RLM_REPOSITORY" HEAD >/dev/null 2>&1 ||
      fail "Cannot reach the PredictRLM repository on GitHub. Check network access."
  fi

  printf 'Checking Python 3.13 availability through UV...\n'
  uv python find 3.13 >/dev/null 2>&1 || uv python install 3.13

  local parent
  parent=$(dirname "$workspace_root")
  staging_root=$(mktemp -d "$parent/.avalanche-init.XXXXXX") ||
    fail "Cannot create a staging directory beside $workspace_root."
}

write_starter_flow() {
  local flow_path="$staging_root/src/$STARTER_WORKFLOW_NAME/flow.py"
  mkdir -p "$(dirname "$flow_path")"
  cat >"$flow_path" <<'EOF'
"""Starter Avalanche flow: a source, a plain step, an agent step and a dest."""

import avalanche as ava


@ava.source
def create_topic() -> str:
    """Source node: starts the flow by creating the topic "hello world"."""
    return "hello world"


@ava.step
def prepare_request(topic: str) -> str:
    """Step node: plain Python that turns the topic into a request for the agent."""
    return f"Write a greeting about {topic}."


@ava.agent_step(
    ava.Signature(
        "request: str -> greeting: str",
        "Agent step: answer the request with one friendly sentence.",
    ),
    runtime=ava.runtimes.PredictRLM(
        lm="openai/gpt-5.5", sub_lm="openai/gpt-5.5", max_iterations=5
    ),
)
async def write_greeting(request: str, *, agent: ava.Agent) -> str:
    """Agent step: asks the model to write a greeting that answers the request."""
    return (await agent(request=request)).outputs.greeting


@ava.dest
def print_greeting(greeting: str) -> str:
    """Dest node: ends the flow by printing the finished greeting and returning it."""
    print(greeting)
    return greeting


@ava.workflow
def start_flow():
    """Prepare a request, have an agent write a greeting, then print it."""
    return create_topic() >> prepare_request() >> write_greeting() >> print_greeting()
EOF
}

write_workspace_readme() {
  cat >"$staging_root/README.md" <<'EOF'
# Avalanche workspace

This project holds your [Avalanche](https://github.com/Trampoline-AI/avalanche)
workflows. Each folder under `src/` is one workflow. It comes with `start_flow`, a
starter flow with a plain Python step and an agent step.

## Run start_flow

`start_flow` calls a real model. Its agent step uses `openai/gpt-5.5` by default, so if
you have an OpenAI key you can run it as is:

```bash
export OPENAI_API_KEY=...
uv run ava dev
```

`OPENAI_API_KEY` is only needed because of that default. To use another provider, open
`src/start_flow/flow.py`, find `runtime=` on `write_greeting`, and change the provider
and model in `lm` and `sub_lm`:

```python
runtime=ava.runtimes.PredictRLM(
    lm="anthropic/claude-sonnet-5", sub_lm="anthropic/claude-sonnet-5", max_iterations=5
),
```

Then set that provider's key instead, and run it the same way:

```bash
export ANTHROPIC_API_KEY=...
uv run ava dev
```

[PredictRLM](#predictrlm) lists the other providers and their keys.

To keep a key in a file, put it in `.env`, which Git ignores, and pass that file to
`uv run`. A plain `uv run ava dev` does not read `.env` for agent steps.

```bash
uv run --env-file .env ava dev
```

Open the browser UI at the address the command prints, select `start_flow` and click
**Run**. You can look at the flow without a key, but a run stops at the agent step.

Zoom in on the graph until the node cards show their details. Each card then says what
kind of node it is and what it does. That text is the first line of the function's
docstring; on an agent step, it is the first line of the signature's instructions.

| Node | Type | What it does |
| --- | --- | --- |
| `create_topic` | Source: starts a flow | Creates the topic `hello world` |
| `prepare_request` | Step: runs plain Python | Turns the topic into a request |
| `write_greeting` | Agent step: gives a task to a model | Writes a greeting |
| `print_greeting` | Dest: ends a flow | Prints the greeting and returns it |

`>>` connects the nodes in `src/start_flow/flow.py`: each node receives the previous
node's output as its first argument.

## Choose a runtime and model

An agent step runs on one of three runtimes. To change what `start_flow` uses, edit
`runtime=` on `write_greeting` in `src/start_flow/flow.py` and set the credentials that
runtime needs.

| Runtime | Install | Models | Credentials |
| --- | --- | --- | --- |
| `ava.runtimes.PredictRLM` | Included | Many providers, as `provider/model` | The provider's API key |
| `ava.runtimes.ClaudeAgent` | `uv add 'avalanche-ai[claude]'` | Claude only | A `claude` login, `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` |
| `ava.runtimes.OpenAIAgent` | `uv add 'avalanche-ai[openai]'` | OpenAI only | `OPENAI_API_KEY` |

### PredictRLM

The agent writes Python and runs it in a sandbox. Nothing extra needs installing.

```python
ava.runtimes.PredictRLM(lm="anthropic/claude-sonnet-5", sub_lm="anthropic/claude-haiku-4-5")
```

`lm` is the main model and `sub_lm` is a second, usually smaller model the agent can
ask for structured answers. Both take `provider/model` IDs, which follow the naming
in [LiteLLM's provider list](https://docs.litellm.ai/docs/providers). Look up model
IDs there, then check how the prefix is served.

These prefixes work as installed, because DSPy, the library PredictRLM calls models
with, talks to them directly:

| Prefix | Provider | Credential |
| --- | --- | --- |
| `openai/` | OpenAI | `OPENAI_API_KEY` |
| `anthropic/` | Anthropic | `ANTHROPIC_API_KEY` |
| `gemini/` | Google Gemini | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| `moonshot/` | Moonshot AI (Kimi) | `MOONSHOT_API_KEY` |
| `xai/` | xAI | `XAI_API_KEY` |
| `groq/` | Groq | `GROQ_API_KEY` |
| `deepseek/` | DeepSeek | `DEEPSEEK_API_KEY` |
| `openrouter/` | OpenRouter | `OPENROUTER_API_KEY` |
| `ollama/` | Local Ollama server | None |

Every other prefix in LiteLLM's list, such as `bedrock/` or `vertex_ai/`, needs
LiteLLM itself, which this workspace leaves out on purpose. To use one, delete
`exclude-dependencies = ["litellm"]` from `pyproject.toml`, then run:

```bash
uv add 'dspy[litellm]'
```

### ClaudeAgent

Runs Claude through the Claude Agent SDK. `model` is a Claude model ID; leave it out
to let the Claude CLI choose.

```bash
uv add 'avalanche-ai[claude]'
```

```python
ava.runtimes.ClaudeAgent(model="claude-haiku-5-5", builtin_tools=["Read", "Grep"])
```

The extra includes the Claude Code CLI. It signs in as it does in a terminal: with a
`claude` login, `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, or
`ANTHROPIC_API_KEY`.

### OpenAIAgent

Runs OpenAI models through the OpenAI Agents SDK. `model` is an OpenAI model ID,
without a provider prefix; leave it out to use `OPENAI_DEFAULT_MODEL` or else the
SDK's default.

```bash
uv add 'avalanche-ai[openai]'
```

```python
ava.runtimes.OpenAIAgent(model="gpt-5.4-mini", max_turns=6)
```

It reads `OPENAI_API_KEY` from the environment, and `OPENAI_BASE_URL` when set.
EOF
}

write_workspace_guidance() {
  cat >"$staging_root/AGENTS.md" <<'EOF'
# Avalanche workflow workshop

## Purpose

This repository is one UV workspace for a collection of Avalanche workflows.
The `start_flow` starter has a source, a plain step, an agent step and a dest.
`README.md` explains the key it needs, how to run it and how to choose a runtime.

```text
src/
├── start_flow/             # starter flow
├── research_assistant/     # future workflow
└── document_reviewer/      # future workflow
```

Each direct child of `src/` is one workflow. Do not add a wrapper package such
as `src/avalanche_workflows/`, and do not create a separate `pyproject.toml` or
virtual environment for each workflow.

`pyproject.toml` configures `src/` as the workspace flow target. The normal
command scans every flow beneath that dedicated directory:

```bash
uv run ava dev
```

To scan one flow instead of the configured workspace target:

```bash
uv run ava dev src/start_flow/flow.py
```

## Deploying to Delta Cloud

The `dev` dependency group installs the `delta` CLI into the workspace
environment; it is not part of the deployed workflow dependencies. Sign in once,
link the workspace to an organization and project, then deploy:

```bash
uv run delta login
uv run delta init --org <ORG_ID> --project proj_<name>   # writes delta.toml once
uv run delta deploy
uv run delta flows list
uv run delta runs start --flow <FLOW> --input '{}'
```
EOF

  if [[ "$use_editable_dependencies" == true ]]; then
    cat >>"$staging_root/AGENTS.md" <<'EOF'

## Editable framework checkouts

`.trampoline-ai/avalanche` and `.trampoline-ai/predict-rlm` are independent Git
repositories and editable dependencies. Make framework changes in those
checkouts and commit them from their own repositories.
EOF
  else
    cat >>"$staging_root/AGENTS.md" <<'EOF'

## Framework dependency

`pyproject.toml` resolves the published `avalanche-ai` package. Use
`uv lock --upgrade-package avalanche-ai` when you want to update it.
EOF
  fi
}

initialize_workspace() {
  uv init --bare --name "$PROJECT_NAME" --python 3.13 --vcs git --no-workspace "$staging_root"
  cat >"$staging_root/pyproject.toml" <<'EOF'
[project]
name = "avalanche-workspace"
version = "0.1.0"
requires-python = ">=3.13,<3.14"
EOF

  if [[ "$use_editable_dependencies" == true ]]; then
    # uv applies a source only to a direct dependency, so the checkout must be listed.
    cat >>"$staging_root/pyproject.toml" <<'EOF'
dependencies = [
    "avalanche-ai>=0.17",
    "predict-rlm",
]
EOF
  else
    cat >>"$staging_root/pyproject.toml" <<'EOF'
dependencies = ["avalanche-ai>=0.17"]
EOF
  fi

  cat >>"$staging_root/pyproject.toml" <<'EOF'

[dependency-groups]
dev = ["trampoline-delta>=0.3.4"]

[tool.avalanche]
flow_targets = ["src"]

[tool.uv]
# Avalanche installs PredictRLM and DSPy. DSPy still declares litellm but calls the main
# providers directly. Routes served only through LiteLLM (for example bedrock/, vertex_ai/)
# need this exclusion removed and dspy[litellm] added.
exclude-dependencies = ["litellm"]
EOF

  if [[ "$use_editable_dependencies" == true ]]; then
    cat >>"$staging_root/pyproject.toml" <<'EOF'

[tool.uv.sources]
avalanche-ai = { path = ".trampoline-ai/avalanche", editable = true }
predict-rlm = { path = ".trampoline-ai/predict-rlm", editable = true }
EOF
    git clone "$AVALANCHE_REPOSITORY" "$staging_root/.trampoline-ai/avalanche"
    git clone "$PREDICT_RLM_REPOSITORY" "$staging_root/.trampoline-ai/predict-rlm"
    printf '.trampoline-ai/\n' >>"$staging_root/.gitignore"
  fi
  [[ -f "$AVALANCHE_SKILL_SOURCE/SKILL.md" ]] ||
    fail "Bundled Avalanche authoring skill is missing: $AVALANCHE_SKILL_SOURCE"
  mkdir -p "$staging_root/.agent/skills"
  cp -R "$AVALANCHE_SKILL_SOURCE" "$staging_root/.agent/skills/avalanche"

  printf '.env\n' >>"$staging_root/.gitignore"

  write_starter_flow
  uv sync --directory "$staging_root"

  write_workspace_readme
  write_workspace_guidance
}

verify_workspace() {
  local root=$1
  (
    cd "$root"
    .venv/bin/python -B -c 'import avalanche; print(avalanche.__file__)'
    uv run ava dev --help >/dev/null

    if [[ "$use_editable_dependencies" == true ]]; then
      .venv/bin/python -B -c '
import avalanche
import predict_rlm
from pathlib import Path

checkouts = ((avalanche, ".trampoline-ai/avalanche"), (predict_rlm, ".trampoline-ai/predict-rlm"))
for module, relative_checkout in checkouts:
    module_path = Path(module.__file__).resolve()
    checkout_path = (Path.cwd() / relative_checkout).resolve()
    if checkout_path not in module_path.parents:
        raise RuntimeError(f"{module.__name__} resolved outside editable checkout: {module_path}")
    print(module_path)
'
    fi
    test -f .agent/skills/avalanche/SKILL.md

    .venv/bin/python -B -c '
import importlib.util
from pathlib import Path

flow_path = Path.cwd() / "src/start_flow/flow.py"
spec = importlib.util.spec_from_file_location("starter_flow", flow_path)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Cannot import starter flow: {flow_path}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.start_flow()
print(flow_path)
'

    .venv/bin/ava operator --help >/dev/null
    .venv/bin/ava run --help >/dev/null
    .venv/bin/delta version >/dev/null
    git check-ignore .env >/dev/null
    test -f AGENTS.md
    test -f README.md
  )
}

commit_workspace() {
  local contents=()
  shopt -s dotglob nullglob
  contents=("$staging_root"/*)
  shopt -u dotglob nullglob
  mv "${contents[@]}" "$workspace_root/"
  rmdir "$staging_root"
  staging_root=""
}

rebuild_workspace_environment() {
  # Virtual-environment entry points embed their installation path.
  uv sync --reinstall --directory "$workspace_root"
}

report_success() {
  printf '\nInitialized and verified %s\n' "$workspace_root"

  if [[ "$use_editable_dependencies" == true ]]; then
    local avalanche_branch
    local predict_rlm_branch
    avalanche_branch=$(git -C "$workspace_root/.trampoline-ai/avalanche" branch --show-current)
    predict_rlm_branch=$(git -C "$workspace_root/.trampoline-ai/predict-rlm" branch --show-current)
    printf 'Avalanche branch: %s\n' "$avalanche_branch"
    printf 'PredictRLM branch: %s\n' "$predict_rlm_branch"
    printf 'Avalanche skill: %s\n' ".agent/skills/avalanche"
  else
    printf 'Avalanche dependency: published avalanche-ai\n'
  fi
  printf '\nThe starter flow uses openai/gpt-5.5 by default. With an OpenAI key, run it as is, then select start_flow in the browser UI:\n'
  printf '  export OPENAI_API_KEY=...\n'
  printf '  uv run ava dev\n'
  printf "To use another provider, change lm and sub_lm in src/start_flow/flow.py and set that provider's key.\n"
  printf 'README.md explains how, and how to choose another runtime.\n'
}

main() {
  parse_args "$@"
  check_prerequisites
  initialize_workspace
  verify_workspace "$staging_root"
  commit_workspace
  rebuild_workspace_environment
  verify_workspace "$workspace_root"
  report_success
}

main "$@"
