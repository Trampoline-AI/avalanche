#!/usr/bin/env python3
"""Create a verified Avalanche workflow workspace from local editable checkouts."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

AVALANCHE_REPOSITORY = "https://github.com/Trampoline-AI/avalanche.git"
PREDICT_RLM_REPOSITORY = "https://github.com/Trampoline-AI/predict-rlm.git"
WORKSPACE_NAME = "avalanche-workflows"
STARTER_WORKFLOW_NAME = "binary_converter"
DEFAULT_OPENAI_MODEL = "openai/gpt-5.6-terra"
DEFAULT_CODEX_MODEL = "gpt-5.6-terra"
ASSET_ROOT = Path(__file__).resolve().parent.parent / "assets" / STARTER_WORKFLOW_NAME
REFERENCE_ROOT = Path(__file__).resolve().parent.parent / "references"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    model: str
    credential_env: str | None
    lm_setup: str
    lm_expression: str
    needs_codex_lm: bool


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create an Avalanche workflow workspace with editable framework checkouts."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd() / WORKSPACE_NAME,
        help="Workspace directory. Defaults to ./avalanche-workflows.",
    )
    parser.add_argument(
        "--provider",
        choices=("codex", "openai", "other"),
        default="openai",
        help="Starter workflow model backend. Defaults to openai.",
    )
    parser.add_argument(
        "--model",
        help="Model slug. Required for --provider other.",
    )
    parser.add_argument(
        "--credential-env",
        help="Credential environment variable. Required for --provider other.",
    )
    return parser.parse_args()


def resolve_provider(arguments: argparse.Namespace) -> ProviderConfig:
    if arguments.provider == "codex":
        if arguments.credential_env is not None:
            raise ValueError("--credential-env is not used with --provider codex.")
        model = arguments.model or DEFAULT_CODEX_MODEL
        return ProviderConfig(
            name="CodexLM via ChatGPT/Codex subscription",
            model=model,
            credential_env=None,
            lm_setup=(
                "from dspy_codex_lm import CodexLM\n\n"
                f"CODEX_LM = CodexLM(model={json.dumps(model)})\n"
            ),
            lm_expression="CODEX_LM",
            needs_codex_lm=True,
        )

    if arguments.provider == "openai":
        if arguments.credential_env is not None:
            raise ValueError("--credential-env is not used with --provider openai.")
        model = arguments.model or DEFAULT_OPENAI_MODEL
        return ProviderConfig(
            name="OpenAI API",
            model=model,
            credential_env="OPENAI_API_KEY",
            lm_setup="",
            lm_expression=json.dumps(model),
            needs_codex_lm=False,
        )

    if arguments.model is None:
        raise ValueError("--model is required with --provider other.")
    if arguments.credential_env is None:
        raise ValueError("--credential-env is required with --provider other.")
    return ProviderConfig(
        name="LiteLLM-compatible provider",
        model=arguments.model,
        credential_env=arguments.credential_env,
        lm_setup="",
        lm_expression=json.dumps(arguments.model),
        needs_codex_lm=False,
    )


def required_command(candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if shutil.which(candidate) is not None:
            return candidate
    names = ", ".join(candidates)
    raise RuntimeError(f"Missing required command. Install one of: {names}.")


def run(command: list[str], *, cwd: Path, capture: bool = False) -> str:
    print("+", " ".join(command))
    if not capture:
        subprocess.run(command, cwd=cwd, check=True)
        return ""

    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode != 0:
        command_text = " ".join(command)
        raise RuntimeError(
            f"Command failed with exit status {completed.returncode}: {command_text}"
        )
    return completed.stdout + completed.stderr




def ensure_empty_workspace(workspace_root: Path) -> Path:
    resolved_root = workspace_root.expanduser().resolve()
    if resolved_root.exists() and not resolved_root.is_dir():
        raise ValueError(f"Workspace root is not a directory: {resolved_root}")
    if resolved_root.exists() and any(resolved_root.iterdir()):
        raise ValueError(f"Workspace root is not empty: {resolved_root}")
    resolved_root.parent.mkdir(parents=True, exist_ok=True)
    return resolved_root


def update_python_constraint(workspace_root: Path) -> None:
    pyproject_path = workspace_root / "pyproject.toml"
    content = pyproject_path.read_text()
    expected = 'requires-python = ">=3.11"'
    if content.count(expected) != 1:
        raise RuntimeError(f"Expected one Python requirement entry in {pyproject_path}.")
    pyproject_path.write_text(content.replace(expected, 'requires-python = ">=3.11,<3.14"'))



def add_gitignore_entry(workspace_root: Path) -> None:
    gitignore_path = workspace_root / ".gitignore"
    content = gitignore_path.read_text()
    if ".trampoline-ai/" not in content.splitlines():
        gitignore_path.write_text(f"{content.rstrip()}\n.trampoline-ai/\n")


def write_starter_workflow(workspace_root: Path, provider: ProviderConfig) -> None:
    workflow_root = workspace_root / "src" / STARTER_WORKFLOW_NAME
    workflow_root.mkdir(parents=True)
    asset_path = ASSET_ROOT / "flow.py"
    if not asset_path.is_file():
        raise RuntimeError(f"Starter flow asset is missing: {asset_path}")
    content = asset_path.read_text()
    content = content.replace("# __AGENT_LM_SETUP__\n", provider.lm_setup)
    content = content.replace('"__AGENT_LM__"', provider.lm_expression)
    workflow_root.joinpath("flow.py").write_text(content)


def write_agents_guidance(workspace_root: Path, provider: ProviderConfig) -> None:
    guidance = (REFERENCE_ROOT / "AGENTS.md").read_text()
    guidance = guidance.replace("__STARTER_PROVIDER__", provider.name)
    guidance = guidance.replace("__STARTER_MODEL__", provider.model)
    guidance = guidance.replace(
        "__STARTER_CREDENTIAL__",
        provider.credential_env or "CodexLM subscription authentication",
    )
    workspace_root.joinpath("AGENTS.md").write_text(guidance)




def verify_workspace(workspace_root: Path, package_runner: str, uv: str) -> None:
    import_check = "\n".join(
        [
            "import avalanche",
            "import predict_rlm",
            "from pathlib import Path",
            "checkouts = ((avalanche, '.trampoline-ai/avalanche'),",
            "             (predict_rlm, '.trampoline-ai/predict-rlm'))",
            "for module, relative_checkout in checkouts:",
            "    module_path = Path(module.__file__).resolve()",
            "    checkout_path = (Path.cwd() / relative_checkout).resolve()",
            "    if checkout_path not in module_path.parents:",
            "        raise RuntimeError(",
            "            f'{module.__name__} resolved outside editable checkout: '",
            "            f'{module_path}'",
            "        )",
            "    print(module_path)",
        ]
    )
    run(["uv", "run", "python", "-B", "-c", import_check], cwd=workspace_root, capture=True)

    flow_check = "\n".join(
        [
            "import importlib.util",
            "from pathlib import Path",
            f"flow_path = Path.cwd() / 'src/{STARTER_WORKFLOW_NAME}/flow.py'",
            "spec = importlib.util.spec_from_file_location('starter_flow', flow_path)",
            "if spec is None or spec.loader is None:",
            "    raise RuntimeError(f'Cannot import starter flow: {flow_path}')",
            "module = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(module)",
            "print(flow_path)",
        ]
    )
    run(["uv", "run", "python", "-B", "-c", flow_check], cwd=workspace_root, capture=True)
    run([uv, "run", "ava", "operator", "--help"], cwd=workspace_root, capture=True)
    run([uv, "run", "ava", "run", "--help"], cwd=workspace_root, capture=True)

    run([package_runner, "--yes", "skills", "list"], cwd=workspace_root, capture=True)
    skill_path = workspace_root / ".agents" / "skills" / "avalanche" / "SKILL.md"
    if not skill_path.is_file():
        raise RuntimeError("Project-scoped Avalanche skill is not installed.")

    run(
        ["git", "check-ignore", ".trampoline-ai/avalanche/pyproject.toml"],
        cwd=workspace_root,
    )
    run(
        ["git", "-C", ".trampoline-ai/avalanche", "status", "--short", "--branch"],
        cwd=workspace_root,
    )
    run(
        ["git", "-C", ".trampoline-ai/predict-rlm", "status", "--short", "--branch"],
        cwd=workspace_root,
    )

    agents_path = workspace_root / "AGENTS.md"
    if not agents_path.is_file():
        raise RuntimeError("Workspace AGENTS.md is missing.")


def main() -> None:
    arguments = parse_arguments()
    provider = resolve_provider(arguments)
    git = required_command(("git",))
    uv = required_command(("uv",))
    package_runner = required_command(("npx", "pnpx", "bunx"))
    workspace_root = ensure_empty_workspace(arguments.root)

    run(
        [
            uv,
            "init",
            "--bare",
            "--name",
            WORKSPACE_NAME,
            "--python",
            "3.11",
            "--vcs",
            "git",
            "--no-workspace",
            str(workspace_root),
        ],
        cwd=workspace_root.parent,
    )
    update_python_constraint(workspace_root)
    run(
        [git, "clone", AVALANCHE_REPOSITORY, ".trampoline-ai/avalanche"],
        cwd=workspace_root,
    )
    run(
        [git, "clone", PREDICT_RLM_REPOSITORY, ".trampoline-ai/predict-rlm"],
        cwd=workspace_root,
    )
    add_gitignore_entry(workspace_root)
    predict_rlm_add = [
        uv,
        "add",
        "--editable",
        ".trampoline-ai/predict-rlm",
    ]
    if provider.needs_codex_lm:
        predict_rlm_add.extend(["--extra", "codex-lm"])
    predict_rlm_add.append("--no-sync")
    run(predict_rlm_add, cwd=workspace_root)
    run(
        [
            uv,
            "add",
            "--editable",
            ".trampoline-ai/avalanche",
            "--extra",
            "all",
            "--no-sync",
        ],
        cwd=workspace_root,
    )
    run(
        [
            package_runner,
            "--yes",
            "skills",
            "add",
            "./.trampoline-ai/avalanche",
            "--skill",
            "avalanche",
            "--yes",
        ],
        cwd=workspace_root,
    )
    write_starter_workflow(workspace_root, provider)
    write_agents_guidance(workspace_root, provider)
    run([uv, "sync"], cwd=workspace_root)
    verify_workspace(workspace_root, package_runner, uv)
    print(f"Initialized and verified {workspace_root} with {provider.name}.")


if __name__ == "__main__":
    main()
