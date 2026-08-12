#!/usr/bin/env bash
# Create a verified Avalanche demo workspace in the current empty directory.
set -Eeuo pipefail

readonly AVALANCHE_REPOSITORY="https://github.com/Trampoline-AI/avalanche.git"
readonly PREDICT_RLM_REPOSITORY="https://github.com/Trampoline-AI/predict-rlm.git"
readonly PROJECT_NAME="avalanche-workspace"
readonly STARTER_WORKFLOW_NAME="binary_converter"

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
  cat >"$flow_path" <<'EOF'
import random

import avalanche as ava

# Managed by scripts/configure-provider.sh. Do not place credentials here.
STARTER_PROVIDER = "unconfigured"
STARTER_MODEL = ""


def starter_lm():
    if STARTER_PROVIDER == "codex-lm":
        from dspy_codex_lm import CodexLM

        return CodexLM(model=STARTER_MODEL)
    return STARTER_MODEL


@ava.source
def generate_binary() -> str:
    length = random.randint(128, 256)
    return "1" + "".join(random.choice("01") for _ in range(length - 1))


@ava.agent_step(
    ava.Signature("binary: str -> decimal: str"),
    lm=starter_lm(),
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

write_provider_configurator() {
  local setup_path="$staging_root/scripts/configure-provider.sh"
  mkdir -p "$(dirname "$setup_path")"
  cat >"$setup_path" <<'EOF'
#!/usr/bin/env bash
# Configure the model provider for the workspace starter flow.
set -Eeuo pipefail

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
flow_path="$workspace_root/src/binary_converter/flow.py"
provider_name=""
provider_key=""
model=""
credential_env=""
api_key=""
needs_codex_lm=false

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  local command=$1
  command -v "$command" >/dev/null 2>&1 || fail "Missing required command: $command"
}

require_interactive_terminal() {
  [[ -t 0 && -t 1 ]] || fail "Provider setup requires a terminal. Run bash scripts/configure-provider.sh from an interactive shell."
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

choose_litellm_model() {
  while true; do
    read -r -p 'LiteLLM model string (for example, provider/model): ' model
    [[ -n "$model" ]] && return
    printf 'A LiteLLM model string is required.\n' >&2
  done
}

choose_credential_env() {
  while true; do
    read -r -p 'Credential environment variable (leave blank if none is required): ' credential_env
    [[ -z "$credential_env" || "$credential_env" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && return
    printf 'Use a valid environment-variable name.\n' >&2
  done
}

select_provider() {
  local choice
  printf 'Avalanche provider setup\n\n'
  printf 'Choose a model provider:\n'
  printf '  1) CodexLM — ChatGPT/Codex subscription\n'
  printf '  2) OpenAI API\n'
  printf '  3) Anthropic API\n'
  printf '  4) Gemini API\n'
  printf '  5) Kimi (Moonshot AI) API\n'
  printf '  6) Other LiteLLM-compatible API\n'
  choice=$(choose_option 'Selection [1-6]: ')

  case "$choice" in
  1)
    provider_name="CodexLM via ChatGPT/Codex subscription"
    provider_key="codex-lm"
    model=$(choose_model 'Selection [1]: ' "gpt-5.6-terra")
    needs_codex_lm=true
    ;;
  2)
    provider_name="OpenAI API"
    provider_key="litellm"
    model=$(choose_model 'Selection [1-4]: ' "openai/gpt-5.6-terra" "openai/gpt-5.6-sol" "openai/gpt-5.6-luna" "openai/gpt-5.5")
    credential_env="OPENAI_API_KEY"
    ;;
  3)
    provider_name="Anthropic API"
    provider_key="litellm"
    model=$(choose_model 'Selection [1-3]: ' "anthropic/claude-sonnet-5" "anthropic/claude-opus-5" "anthropic/claude-haiku-4-5")
    credential_env="ANTHROPIC_API_KEY"
    ;;
  4)
    provider_name="Gemini API"
    provider_key="litellm"
    model=$(choose_model 'Selection [1-4]: ' "gemini/gemini-2.5-pro" "gemini/gemini-2.0-flash" "gemini/gemini-3.5-flash" "gemini/gemini-3.6-flash")
    credential_env="GEMINI_API_KEY"
    ;;
  5)
    provider_name="Kimi (Moonshot AI) API"
    provider_key="litellm"
    model=$(choose_model 'Selection [1-3]: ' "moonshot/kimi-k3" "moonshot/kimi-k2.7-code" "moonshot/kimi-k2.6")
    credential_env="MOONSHOT_API_KEY"
    ;;
  6)
    provider_name="Other LiteLLM-compatible API"
    provider_key="litellm"
    choose_litellm_model
    choose_credential_env
    ;;
  esac
}

collect_api_key() {
  [[ -n "$credential_env" ]] || return 0

  printf '\nPaste your %s key now. Input is hidden and it will be saved only in .env.\n' "$provider_name"
  while true; do
    read -r -s -p 'API key: ' api_key
    printf '\n'
    [[ -n "$api_key" ]] && return
    printf 'An API key is required for the selected provider.\n' >&2
  done
}

write_flow_configuration() {
  "$workspace_root/.venv/bin/python" - "$flow_path" "$provider_key" "$model" <<'PY'
import json
import re
import sys
from pathlib import Path

flow_path = Path(sys.argv[1])
provider, model = sys.argv[2:]
text = flow_path.read_text()
for name, value in (("STARTER_PROVIDER", provider), ("STARTER_MODEL", model)):
    text, replacements = re.subn(
        rf"^{name} = .*$",
        f"{name} = {json.dumps(value)}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if replacements != 1:
        raise RuntimeError(f"Cannot find the managed {name} setting in {flow_path}")
flow_path.write_text(text)
PY
}

write_api_key() {
  local env_path="$workspace_root/.env"
  local temporary_path
  local line

  temporary_path=$(mktemp "$workspace_root/.env.XXXXXX") ||
    fail "Cannot create a temporary .env file."
  if [[ -f "$env_path" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" == "$credential_env="* ]] || printf '%s\n' "$line" >>"$temporary_path"
    done <"$env_path"
  fi
  printf '%s=%s\n' "$credential_env" "$api_key" >>"$temporary_path"
  chmod 600 "$temporary_path"
  mv "$temporary_path" "$env_path"
  unset api_key
}

authenticate_codex() {
  [[ "$needs_codex_lm" == true ]] || return 0

  uv sync --no-dev --extra codex-lm --directory "$workspace_root"

  local response
  read -r -p 'Set up Codex subscription authentication now? [Y/n] ' response
  case "$response" in
  "" | y | Y | yes | YES)
    require_command codex
    "$workspace_root/.venv/bin/codex-lm" auth login default --device-auth
    ;;
  *)
    printf 'CodexLM authentication skipped. Before running the flow, run:\n'
    printf '  .venv/bin/codex-lm auth login default --device-auth\n'
    ;;
  esac
}

main() {
  require_interactive_terminal
  [[ -f "$flow_path" ]] || fail "Run this script from an initialized Avalanche workspace."
  [[ -x "$workspace_root/.venv/bin/python" ]] || fail "The workspace virtual environment is missing. Rerun init.sh."

  select_provider
  collect_api_key
  write_flow_configuration
  if [[ -n "$credential_env" ]]; then
    write_api_key
  fi
  authenticate_codex

  printf '\nConfigured %s with %s.\n' "$provider_name" "$model"
  if [[ -n "$credential_env" ]]; then
    printf '.env contains %s and is ignored by Git.\n' "$credential_env"
  fi
  printf '\nStart the demo with:\n'
  printf '  uv run ava dev --flows src/binary_converter/\n'
}

main "$@"
EOF
  chmod 755 "$setup_path"
}

write_workspace_guidance() {
  cat >"$staging_root/AGENTS.md" <<'EOF'
# Avalanche workflow workshop

## Purpose

This repository is one UV workspace for a collection of Avalanche workflows.
The `binary_converter` starter is a statically checked example flow.

```text
src/
├── binary_converter/       # starter flow
├── research_assistant/     # future workflow
└── document_reviewer/      # future workflow
```

Each direct child of `src/` is one workflow. Do not add a wrapper package such
as `src/avalanche_workflows/`, and do not create a separate `pyproject.toml` or
virtual environment for each workflow.

## Starter flow

Reconfigure the model provider at any time with:

```bash
bash scripts/configure-provider.sh
```

The script stores API keys in the Git-ignored `.env` when needed. Do not place
credentials in `src/binary_converter/flow.py`.

## Execution boundary

Flows execute through Avalanche's operator. This workspace contains flow
declarations only. From the workspace root, start the starter flow with:

```bash
uv run ava dev --flows src/binary_converter/
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
  uv init --bare --name "$PROJECT_NAME" --python 3.11 --vcs git --no-workspace "$staging_root"
  cat >"$staging_root/pyproject.toml" <<'EOF'
[project]
name = "avalanche-workspace"
version = "0.1.0"
requires-python = ">=3.11,<3.14"
EOF

  if [[ "$use_editable_dependencies" == true ]]; then
    cat >>"$staging_root/pyproject.toml" <<'EOF'
dependencies = [
    "avalanche-ai",
    "predict-rlm",
]
EOF
  else
    cat >>"$staging_root/pyproject.toml" <<'EOF'
dependencies = [
    "avalanche-ai",
    "predict-rlm",
]
EOF
  fi

  cat >>"$staging_root/pyproject.toml" <<'EOF'

[project.optional-dependencies]
codex-lm = ["predict-rlm[codex-lm]"]
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
  cat >"$staging_root/.env" <<'EOF'
# Managed by scripts/configure-provider.sh. Provider credentials are stored here.
EOF
  chmod 600 "$staging_root/.env"

  uv sync --no-dev --directory "$staging_root"

  write_starter_flow
  write_provider_configurator
  write_workspace_guidance
}

verify_workspace() {
  local root=$1
  (
    cd "$root"
    .venv/bin/python -B -c 'import avalanche; print(avalanche.__file__)'

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
    git check-ignore .env >/dev/null
    test -f AGENTS.md
    test -x scripts/configure-provider.sh
    bash -n scripts/configure-provider.sh
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
  uv sync --reinstall --no-dev --directory "$workspace_root"
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
}

configure_provider() {
  local response

  if [[ ! -t 0 || ! -t 1 ]]; then
    printf '\nConfigure a provider later from an interactive terminal:\n'
    printf '  bash scripts/configure-provider.sh\n'
    return
  fi

  while true; do
    if ! read -r -p 'Configure a provider now? [Y/n] ' response; then
      printf '\nProvider setup skipped. Run when ready:\n'
      printf '  bash scripts/configure-provider.sh\n'
      return
    fi
    case "$response" in
    "" | y | Y | yes | YES)
      bash "$workspace_root/scripts/configure-provider.sh"
      return
      ;;
    n | N | no | NO)
      printf '\nProvider setup skipped. Run when ready:\n'
      printf '  bash scripts/configure-provider.sh\n'
      return
      ;;
    *) printf 'Choose Y or n.\n' >&2 ;;
    esac
  done
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
  configure_provider
}

main "$@"
