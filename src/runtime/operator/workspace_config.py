"""Workspace-scoped default flow target resolution for local CLI commands."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError

from .discovery import ConfiguredRoot, configure_roots


class _AvalancheToolConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    flow_targets: list[StrictStr] | None = None


class _ToolConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    avalanche: _AvalancheToolConfig | None = None


class _PyprojectConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    tool: _ToolConfig | None = None


@dataclass(frozen=True)
class WorkflowTargetSelection:
    """Validated workflow targets and the workspace configuration that supplied them."""

    paths: tuple[str, ...]
    roots: tuple[ConfiguredRoot, ...]
    config_path: Path | None = None


def format_scan_targets(selection: WorkflowTargetSelection) -> tuple[str, ...]:
    """Render the validated absolute targets that discovery will scan."""
    source = (
        "command line"
        if selection.config_path is None
        else f"workspace config: {selection.config_path}"
    )
    return (
        f"  Scan targets ({source}):",
        *(
            f"    - {root.target} " f"({'directory' if root.target.is_dir() else 'file'})"
            for root in selection.roots
        ),
    )


def select_workflow_targets(
    explicit_paths: list[str], *, working_directory: Path | None = None
) -> WorkflowTargetSelection:
    """Resolve explicit targets or safe workspace defaults without a CWD fallback."""
    if explicit_paths:
        roots = configure_roots(explicit_paths)
        return WorkflowTargetSelection(paths=tuple(explicit_paths), roots=roots)

    directory = (working_directory or Path.cwd()).resolve()
    config_path = _nearest_pyproject(directory)
    if config_path is None:
        raise ValueError(
            "No workflow targets configured. Pass FLOW [FLOW ...], or set "
            "[tool.avalanche].flow_targets in pyproject.toml."
        )

    config = _load_workspace_config(config_path)
    if config is None or not config.flow_targets:
        raise ValueError(
            "No workflow targets configured. Pass FLOW [FLOW ...], or set "
            "[tool.avalanche].flow_targets in pyproject.toml."
        )

    paths = tuple(
        _workspace_target_path(value, config_path.parent) for value in config.flow_targets
    )
    try:
        roots = configure_roots(list(paths))
    except ValueError as exc:
        raise ValueError(
            f"Invalid [tool.avalanche].flow_targets in {config_path}: {exc}"
        ) from exc
    return WorkflowTargetSelection(paths=paths, roots=roots, config_path=config_path)


def _nearest_pyproject(directory: Path) -> Path | None:
    for candidate_directory in (directory, *directory.parents):
        candidate = candidate_directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def _load_workspace_config(config_path: Path) -> _AvalancheToolConfig | None:
    try:
        with config_path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"Could not parse workspace configuration {config_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise ValueError(
            f"Could not read workspace configuration {config_path}: {exc}"
        ) from exc
    try:
        config = _PyprojectConfig.model_validate(document)
    except ValidationError as exc:
        raise ValueError(
            f"Invalid [tool.avalanche] configuration in {config_path}: {exc}"
        ) from exc
    if config.tool is None:
        return None
    return config.tool.avalanche


def _workspace_target_path(value: str, workspace_root: Path) -> str:
    alias, raw_path = _split_alias(value)
    if not raw_path.strip():
        raise ValueError("Workspace flow target must not be empty")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = workspace_root / path
    if alias is None:
        return str(path)
    return f"{alias}={path}"


def _split_alias(value: str) -> tuple[str | None, str]:
    if "=" not in value:
        return None, value
    alias, path = value.split("=", 1)
    if alias and path:
        return alias, path
    return None, value
