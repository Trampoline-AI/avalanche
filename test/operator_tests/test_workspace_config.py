"""Workspace selection precedence without CLI copy assertions."""

import pytest

from runtime.operator.workspace_config import select_workflow_targets


def test_workspace_resolution_and_explicit_target_precedence(tmp_path):
    flows = tmp_path / "src"
    nested = flows / "nested"
    nested.mkdir(parents=True)
    config = tmp_path / "pyproject.toml"
    config.write_text('[tool.avalanche]\nflow_targets = ["src"]\n')
    selection = select_workflow_targets([], working_directory=nested)
    assert selection.paths == (str(flows),)

    config.write_text("invalid TOML")
    explicit = select_workflow_targets([str(flows)], working_directory=nested)
    assert explicit.paths == (str(flows),)
    with pytest.raises(ValueError):
        select_workflow_targets([], working_directory=nested)


@pytest.mark.parametrize("targets", ["[]", '["missing.py"]'])
def test_workspace_never_silently_scans_an_unconfigured_target(tmp_path, targets):
    (tmp_path / "pyproject.toml").write_text(f"[tool.avalanche]\nflow_targets = {targets}\n")
    with pytest.raises(ValueError):
        select_workflow_targets([], working_directory=tmp_path)
