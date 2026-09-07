from __future__ import annotations

from runtime.operator import WorkflowRegistry


def test_directory_scan_ignores_excluded_directories(tmp_path):
    visible_flow = tmp_path / "flow.py"
    visible_flow.write_text(
        """
from avalanche import source, workflow


@source
def generate() -> int:
    return 1


@workflow
def visible():
    return generate()
"""
    )
    for directory_name in (".venv", "venv", "build", "node_modules"):
        excluded_flow = tmp_path / directory_name / "dependency.py"
        excluded_flow.parent.mkdir(parents=True)
        excluded_flow.write_text(
            'raise RuntimeError("must not import excluded directory code")'
        )

    registry = WorkflowRegistry()
    registry.scan([str(tmp_path)])

    assert [workflow.name for workflow in registry.list_workflows()] == ["visible"]
    assert registry.view.diagnostics == ()
