#!/usr/bin/env bash
# Create a verified Avalanche demo workspace in the current empty directory.
set -Eeuo pipefail

readonly AVALANCHE_REPOSITORY="https://github.com/Trampoline-AI/avalanche.git"
readonly PREDICT_RLM_REPOSITORY="https://github.com/Trampoline-AI/predict-rlm.git"
readonly PROJECT_NAME="avalanche-workspace"
readonly STARTER_WORKFLOW_NAME="binary_converter"

workspace_root="$(pwd -P)"
staging_root=""
provider_name=""
model=""
credential_env=""
needs_codex_lm=false
needs_provider_configuration=false
api_key=""

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

require_interactive_terminal() {
  [[ -t 0 && -t 1 ]] || fail "Interactive setup requires a terminal. Run init.sh from an interactive shell."
}

require_empty_directory() {
  local entries=()
  shopt -s dotglob nullglob
  entries=("$workspace_root"/*)
  shopt -u dotglob nullglob
  ((${#entries[@]} == 0)) || fail "Current directory is not empty: $workspace_root
Create and enter an empty directory, then rerun init.sh."
}

choose_option() {
  local prompt=$1
  local choice
  while true; do
    read -r -p "$prompt" choice
    case "$choice" in
    1 | 2 | 3 | 4 | 5 | 6)
      printf '%s\n' "$choice"
      return
      ;;
    *) printf 'Choose one of the listed options.\n' >&2 ;;
    esac
  done
}

choose_model() {
  local prompt=$1
  shift
  local models=("$@")
  local choice
  local index

  while true; do
    printf '\nChoose a model:\n' >&2
    for index in "${!models[@]}"; do
      printf '  %d) %s\n' "$((index + 1))" "${models[index]}" >&2
    done
    read -r -p "$prompt" choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && ((choice >= 1 && choice <= ${#models[@]})); then
      printf '%s\n' "${models[choice - 1]}"
      return
    fi
    printf 'Choose one of the listed models.\n' >&2
  done
}



select_provider() {
  local choice
  printf 'Avalanche demo workspace setup\n\n'
  printf 'Choose a model provider:\n'
  printf '  1) CodexLM — ChatGPT/Codex subscription\n'
  printf '  2) OpenAI API\n'
  printf '  3) Anthropic API\n'
  printf '  4) Gemini API\n'
  printf '  5) Kimi (Moonshot AI) API\n'
  printf '  6) Other LiteLLM-compatible API (configure later)\n'
  choice=$(choose_option 'Selection [1-6]: ')

  case "$choice" in
  1)
    provider_name="CodexLM via ChatGPT/Codex subscription"
    model=$(choose_model 'Selection [1]: ' "gpt-5.6-terra")
    needs_codex_lm=true
    ;;
  2)
    provider_name="OpenAI API"
    model=$(choose_model 'Selection [1-4]: ' "openai/gpt-5.6-terra" "openai/gpt-5.6-sol" "openai/gpt-5.6-luna" "openai/gpt-5.5")
    credential_env="OPENAI_API_KEY"
    ;;
  3)
    provider_name="Anthropic API"
    model=$(choose_model 'Selection [1-3]: ' "anthropic/claude-sonnet-5" "anthropic/claude-opus-5" "anthropic/claude-haiku-4-5")
    credential_env="ANTHROPIC_API_KEY"
    ;;
  4)
    provider_name="Gemini API"
    model=$(choose_model 'Selection [1-4]: ' "gemini/gemini-2.5-pro" "gemini/gemini-2.0-flash" "gemini/gemini-3.5-flash" "gemini/gemini-3.6-flash")
    credential_env="GEMINI_API_KEY"
    ;;
  5)
    provider_name="Kimi (Moonshot AI) API"
    model=$(choose_model 'Selection [1-3]: ' "moonshot/kimi-k3" "moonshot/kimi-k2.7-code" "moonshot/kimi-k2.6")
    credential_env="MOONSHOT_API_KEY"
    ;;
  6)
    provider_name="Other LiteLLM-compatible API"
    needs_provider_configuration=true
    ;;
  esac
}

collect_api_key() {
  [[ -n "$credential_env" ]] || return 0

  printf '\nPaste your %s key now. Input is hidden and it will be saved only in .env.\n' "$provider_name"
  while true; do
    read -r -s -p 'API key: ' api_key
    printf '\n'
    [[ -n "$api_key" ]] && return 0
    printf 'An API key is required for the selected provider.\n' >&2
  done
}

check_prerequisites() {
  require_interactive_terminal
  require_empty_directory
  require_command git
  require_command uv

  printf 'Checking GitHub access...\n'
  git ls-remote --exit-code "$AVALANCHE_REPOSITORY" HEAD >/dev/null 2>&1 ||
    fail "Cannot reach the Avalanche repository on GitHub. Check network access."
  git ls-remote --exit-code "$PREDICT_RLM_REPOSITORY" HEAD >/dev/null 2>&1 ||
    fail "Cannot reach the PredictRLM repository on GitHub. Check network access."

  printf 'Checking Python 3.11 availability through UV...\n'
  uv python find 3.11 >/dev/null 2>&1 || uv python install 3.11

  local parent
  parent=$(dirname "$workspace_root")
  staging_root=$(mktemp -d "$parent/.avalanche-init.XXXXXX") ||
    fail "Cannot create a staging directory beside $workspace_root."
}

write_starter_flow() {
  local flow_path="$staging_root/src/$STARTER_WORKFLOW_NAME/flow.py"
  mkdir -p "$(dirname "$flow_path")"

  if [[ "$needs_codex_lm" == true ]]; then
    cat >"$flow_path" <<EOF
import random

import avalanche as ava
from dspy_codex_lm import CodexLM

CODEX_LM = CodexLM(model="$model")


@ava.source
def generate_binary() -> str:
    length = random.randint(128, 256)
    return "1" + "".join(random.choice("01") for _ in range(length - 1))


@ava.agent_step(
    ava.Signature("binary: str -> decimal: str"),
    lm=CODEX_LM,
)
async def convert_binary(binary: str, *, agent: ava.Agent) -> str:
    return (await agent(binary=binary)).decimal


@ava.dest
def print_result(result: str) -> str:
    print(result)
    return result


@ava.workflow
def binary_converter():
    return generate_binary() >> convert_binary() >> print_result()
EOF
    return
  fi

  cat >"$flow_path" <<EOF
import random

import avalanche as ava

@ava.source
def generate_binary() -> str:
    length = random.randint(128, 256)
    return "1" + "".join(random.choice("01") for _ in range(length - 1))


@ava.agent_step(
    ava.Signature("binary: str -> decimal: str"),
    lm="$model",
)
async def convert_binary(binary: str, *, agent: ava.Agent) -> str:
    return (await agent(binary=binary)).decimal


@ava.dest
def print_result(result: str) -> str:
    print(result)
    return result


@ava.workflow
def binary_converter():
    return generate_binary() >> convert_binary() >> print_result()
EOF
}
write_workspace_guidance() {
  local credential_note
  local starter_flow_note
  if [[ "$needs_provider_configuration" == true ]]; then
    starter_flow_note="\`src/binary_converter/flow.py\` is intentionally unconfigured."
    credential_note=$'Before running:\n\n1. In `src/binary_converter/flow.py`, replace the empty `lm=""` field with the full LiteLLM model string, for example `provider/model`.\n2. In `.env`, add the API key using that provider’s required environment-variable name, for example `OPENAI_API_KEY=...`.\n\nBoth fields are intentionally empty until you choose a provider.'
  elif [[ "$needs_codex_lm" == true ]]; then
    starter_flow_note="\`src/binary_converter/flow.py\` uses the selected model \`$model\` through **$provider_name**."
    credential_note="Complete CodexLM subscription authentication before operator execution."
  else
    starter_flow_note="\`src/binary_converter/flow.py\` uses the selected model \`$model\` through **$provider_name**."
    credential_note="The selected API key is stored in .env and ignored by Git."
  fi

  cat >"$staging_root/AGENTS.md" <<EOF
# Avalanche workflow workshop

## Purpose

This repository is one UV workspace for a collection of Avalanche workflows.
It was created by Avalanche's \`init.sh\` bootstrapper. The starter
\`binary_converter\` is a real agentic workflow copied from the Avalanche README
and statically checked during setup. For agent-assisted authoring, use the
project-local Avalanche skill at \`.agent/skills/avalanche\`.

\`\`\`text
src/
├── binary_converter/       # starter flow
├── research_assistant/     # future workflow
└── document_reviewer/      # future workflow
\`\`\`

Each direct child of \`src/\` is one workflow. Do not add a wrapper package such
as \`src/avalanche_workflows/\`, and do not create a separate \`pyproject.toml\`,
virtual environment, or framework checkout for each workflow.

## Starter flow

$starter_flow_note

$credential_note

## Execution boundary

Flows execute through Avalanche's operator. This workspace contains flow
declarations only. From the workspace root, start the starter flow with:

\`\`\`bash
uv run ava operator --flows src/binary_converter/ --web
\`\`\`

## Editable framework checkouts

The workspace intentionally uses local editable checkouts:

\`\`\`text
.trampoline-ai/
├── avalanche/       # Avalanche workflow runtime and authoring integration
└── predict-rlm/     # PredictRLM agent runtime used by Avalanche agent steps
\`\`\`

The outer workspace ignores \`.trampoline-ai/\`, but each child directory is an
ordinary independent Git repository. The checkouts are not submodules. Their
files are editable dependencies through \`pyproject.toml\`, so a change in either
checkout is used by the next workspace Python invocation without publishing a
package release.

## Framework contribution policy

When a problem belongs in a local framework checkout, classify it before changing
framework code.

### Bugs: direct fix and pull request

A bug is reproducible behavior that violates an existing contract, documented
behavior, or established expectation. Work on the relevant local checkout:

1. Identify whether the behavior belongs to Avalanche or PredictRLM.
2. Reproduce it with a focused test or minimal command in that repository.
3. Make the smallest correct fix and add or update behavior-level regression coverage.
4. Run focused verification in that checkout.
5. Commit from the affected nested repository, not from this outer workspace.
6. Open a pull request against the corresponding upstream repository.

### Missing features: issue first

A missing capability, new API, changed behavior, or feature request is not a
bug. File an upstream issue describing the user need, proposed behavior,
constraints, and acceptance criteria. Create a feature pull request only when
that issue is linked and implementation was explicitly requested.

### Remote ownership

Use a branch in the nested checkout. If the upstream remote is not writable,
create or use a fork as \`origin\`, retain the official repository as \`upstream\`,
push the branch to the fork, and open the pull request to \`upstream/main\`.

## Git ownership

- Commit workspace and workflow changes from this repository.
- Commit Avalanche changes from \`.trampoline-ai/avalanche\`.
- Commit PredictRLM changes from \`.trampoline-ai/predict-rlm\`.

Never mix those histories or commit nested-repository files into the outer
workspace.
EOF
}


initialize_workspace() {
  uv init --bare --name "$PROJECT_NAME" --python 3.11 --vcs git --no-workspace "$staging_root"
  cat >"$staging_root/pyproject.toml" <<'EOF'
[project]
name = "avalanche-workspace"
version = "0.1.0"
requires-python = ">=3.11,<3.14"
dependencies = [
    "avalanche-ai[all]",
    "predict-rlm",
]

[tool.uv.sources]
avalanche-ai = { path = ".trampoline-ai/avalanche", editable = true }
predict-rlm = { path = ".trampoline-ai/predict-rlm", editable = true }
EOF
  git clone "$AVALANCHE_REPOSITORY" "$staging_root/.trampoline-ai/avalanche"
  git clone "$PREDICT_RLM_REPOSITORY" "$staging_root/.trampoline-ai/predict-rlm"
  local authoring_skill="$staging_root/.trampoline-ai/avalanche/.agents/skills/avalanche"
  [[ -f "$authoring_skill/SKILL.md" ]] || fail "Avalanche authoring skill is missing from the cloned checkout."
  mkdir -p "$staging_root/.agent/skills"
  cp -R "$authoring_skill" "$staging_root/.agent/skills/avalanche"
  printf '.trampoline-ai/\n' >>"$staging_root/.gitignore"
  printf '.env\n' >>"$staging_root/.gitignore"
  if [[ "$needs_provider_configuration" == true ]]; then
    cat >"$staging_root/.env" <<'EOF'
# Add the API key required by the provider you choose, for example:
# OPENAI_API_KEY=
EOF
  elif [[ -n "$credential_env" ]]; then
    printf '%s=%s\n' "$credential_env" "$api_key" >"$staging_root/.env"
    unset api_key
  fi

  if [[ "$needs_codex_lm" == true ]]; then
    cat >>"$staging_root/pyproject.toml" <<'EOF'

[project.optional-dependencies]
codex-lm = ["predict-rlm[codex-lm]"]
EOF
    uv sync --no-dev --extra codex-lm --directory "$staging_root"
  else
    uv sync --no-dev --directory "$staging_root"
  fi

  write_starter_flow
  write_workspace_guidance
}

verify_workspace() {
  (
    cd "$staging_root"
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

    .venv/bin/python -B -c '
import importlib.util
from pathlib import Path

flow_path = Path.cwd() / "src/binary_converter/flow.py"
spec = importlib.util.spec_from_file_location("starter_flow", flow_path)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Cannot import starter flow: {flow_path}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(flow_path)
'

    .venv/bin/ava operator --help >/dev/null
    .venv/bin/ava run --help >/dev/null
    git check-ignore .trampoline-ai/avalanche/pyproject.toml >/dev/null
    test -f .agent/skills/avalanche/SKILL.md
    if [[ -n "$credential_env" ]]; then
      test -f .env
      git check-ignore .env >/dev/null
    fi
    test -f AGENTS.md
  )
}

authenticate_codex() {
  [[ "$needs_codex_lm" == true ]] || return 0

  local response
  read -r -p 'Set up Codex subscription authentication now? [Y/n] ' response
  case "$response" in
  "" | y | Y | yes | YES)
    require_command codex
    "$staging_root/.venv/bin/codex-lm" auth login default --device-auth
    ;;
  *)
    printf 'CodexLM authentication skipped. Before running the flow, run:\n'
    printf '  .venv/bin/codex-lm auth login default --device-auth\n'
    ;;
  esac
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

report_success() {
  local avalanche_branch
  local predict_rlm_branch
  avalanche_branch=$(git -C "$workspace_root/.trampoline-ai/avalanche" branch --show-current)
  predict_rlm_branch=$(git -C "$workspace_root/.trampoline-ai/predict-rlm" branch --show-current)

  printf '\nInitialized and verified %s\n' "$workspace_root"
  printf 'Provider: %s\n' "$provider_name"
  printf 'Model: %s\n' "$model"
  printf 'Avalanche skill: %s\n' ".agent/skills/avalanche"

  if [[ "$needs_provider_configuration" == true ]]; then
    printf 'Provider setup is deferred. Set lm="" in src/binary_converter/flow.py and the provider API key in .env before running.\n'
  elif [[ -n "$credential_env" ]]; then
    printf '.env contains %s and is ignored by Git.\n' "$credential_env"
  fi
  printf '\nStart the demo with:\n'
  printf '  uv run ava operator --flows src/binary_converter/ --web\n'
}

main() {
  check_prerequisites
  select_provider
  collect_api_key
  initialize_workspace
  verify_workspace
  authenticate_codex
  commit_workspace
  report_success
}

main "$@"
