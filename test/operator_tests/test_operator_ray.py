import time
from pathlib import Path

import pytest

from runtime.operator import Operator
from runtime.operator.models import RunStatus


def _wait_terminal(operator: Operator, run_id: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = operator.get_run(run_id)
        if run is not None and run.status in {
            RunStatus.SUCCESS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


def _write_package_workflow(root: Path, value: int, *, fail: bool = False) -> Path:
    package = root / "pkg"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("")
    (package / "helper.py").write_text(f"VALUE = {value}\n")
    failure = "raise RuntimeError('ray boom')" if fail else "return eager, deferred"
    workflow = package / "flow.py"
    workflow.write_text(
        "import importlib\n"
        "import sys\n"
        "import avalanche as ava\n"
        "from .helper import VALUE as EAGER\n"
        "@ava.source\n"
        "def read(log=ava.Logger()):\n"
        "    deferred = importlib.import_module('pkg.helper').VALUE\n"
        "    eager = EAGER\n"
        "    print(f'stdout={eager}:{deferred}')\n"
        "    print(f'stderr={eager}:{deferred}', file=sys.stderr)\n"
        "    log.info(f'logger={eager}:{deferred}')\n"
        f"    {failure}\n"
        "@ava.workflow\n"
        "def flow():\n"
        "    return read()\n"
    )
    return workflow


@pytest.mark.ray
def test_ray_operator_uses_live_source_for_imports_logs_and_later_runs(tmp_path):
    pytest.importorskip("ray")
    workflow = _write_package_workflow(tmp_path, 1)
    operator = Operator(
        [str(workflow)],
        executor_backend="ray",
        watch=False,
        schedule=False,
        prepare_timeout=30.0,
    )
    try:
        first_id = operator.start_run("flow")
        first = _wait_terminal(operator, first_id)
        assert first.status == RunStatus.SUCCESS
        assert {entry.node_id for entry in first.logs if "=1:1" in entry.message} == {"read_1"}
        assert any("stdout=1:1" in entry.message for entry in first.logs)
        assert any("stderr=1:1" in entry.message for entry in first.logs)
        assert any("logger=1:1" in entry.message for entry in first.logs)

        _write_package_workflow(tmp_path, 2)
        operator._refresh_workflows()
        second_id = operator.start_run("flow")
        second = _wait_terminal(operator, second_id)
        assert second.status == RunStatus.SUCCESS
        assert operator.get_run_result(first_id) == (1, 1)
        assert operator.get_run_result(second_id) == (2, 2)

        cancelled_id = operator.start_run("flow")
        operator.cancel_run(cancelled_id)
        cancelled = _wait_terminal(operator, cancelled_id)
        assert cancelled.status == RunStatus.CANCELLED

        _write_package_workflow(tmp_path, 3, fail=True)
        operator._refresh_workflows()
        failed_id = operator.start_run("flow")
        failed = _wait_terminal(operator, failed_id)
        assert failed.status == RunStatus.FAILED
        assert any("ray boom" in entry.message for entry in failed.logs)
    finally:
        operator.close()
