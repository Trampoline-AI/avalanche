from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

CANONICAL_EXAMPLES = [
    Path("examples/complex_dag_pattern.py"),
    Path("examples/stream_pattern.py"),
    Path("examples/cursor_pattern.py"),
    Path("examples/operator_workflow.py"),
]


@pytest.mark.parametrize("example_path", CANONICAL_EXAMPLES, ids=str)
def test_canonical_example_executes(example_path: Path, tmp_path: Path):
    env = os.environ.copy()
    env["AVALANCHE_EXAMPLE_ROOT"] = str(tmp_path / example_path.stem)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")])

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / example_path)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, (
        f"{example_path} failed with exit code {result.returncode}\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )


def test_operator_workflow_example_is_discoverable():
    from runtime.operator.registry import WorkflowRegistry

    registry = WorkflowRegistry()
    registry.scan([str(REPO_ROOT / "examples" / "operator_workflow.py")])

    assert [workflow.name for workflow in registry.list_workflows()] == [
        "operator_demo_workflow"
    ]
