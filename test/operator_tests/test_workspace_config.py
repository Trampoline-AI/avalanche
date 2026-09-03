"""Tests for workspace-configured workflow targets."""

from pathlib import Path

import pytest

from runtime.operator.workspace_config import format_scan_targets, select_workflow_targets


def test_workspace_targets_resolve_relative_to_nearest_pyproject(tmp_path):
    workspace = tmp_path / "workspace"
    flows = workspace / "src"
    flows.mkdir(parents=True)
    nested = flows / "nested"
    nested.mkdir()
    config_path = workspace / "pyproject.toml"
    config_path.write_text('[tool.avalanche]\nflow_targets = ["src"]\n')

    selection = select_workflow_targets([], working_directory=nested)

    assert selection.config_path == config_path
    assert selection.paths == (str(flows),)
    assert selection.roots[0].target == flows.resolve()
    assert format_scan_targets(selection) == (
        f"  Scan targets (workspace config: {config_path}):",
        f"    - {flows.resolve()} (directory)",
    )


def test_explicit_targets_do_not_read_workspace_configuration(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    flow = workspace / "flow.py"
    flow.write_text("import avalanche as ava\n")
    (workspace / "pyproject.toml").write_text("not valid TOML")

    selection = select_workflow_targets([str(flow)], working_directory=workspace)

    assert selection.config_path is None
    assert selection.paths == (str(flow),)
    assert selection.roots[0].target == flow.resolve()


@pytest.mark.parametrize(
    "contents",
    (
        "[project]\nname = 'workspace'\n",
        "[tool.avalanche]\nflow_targets = []\n",
    ),
)
def test_missing_workspace_targets_requires_explicit_flow(tmp_path, contents):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(contents)

    with pytest.raises(ValueError, match="No workflow targets configured"):
        select_workflow_targets([], working_directory=workspace)


def test_invalid_workspace_target_identifies_its_configuration(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = workspace / "pyproject.toml"
    config_path.write_text('[tool.avalanche]\nflow_targets = ["missing.py"]\n')

    with pytest.raises(ValueError, match=str(config_path)):
        select_workflow_targets([], working_directory=workspace)


def test_unresolved_workspace_home_directory_target_is_a_validation_error(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        '[tool.avalanche]\nflow_targets = ["~missing-user/flow.py"]\n'
    )

    def raise_unresolved_home_directory(_path: Path) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", raise_unresolved_home_directory)

    with pytest.raises(ValueError, match="unresolved home directory"):
        select_workflow_targets([], working_directory=workspace)
