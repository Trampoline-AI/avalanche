"""Discovery isolation, cache invalidation, and atomic catalog recovery."""

import os
import signal
import sys
import time

import pytest

from runtime.operator import Operator
from runtime.operator.discovery import WorkflowDiscoveryTimeoutError
from runtime.operator.models import RunStatus
from runtime.operator.registry import AmbiguousWorkflow, WorkflowRegistry


def test_same_named_packages_and_workflows_execute_in_their_own_roots(tmp_path):
    roots = []
    for label in ("left", "right"):
        root = tmp_path / label
        package = root / "shared"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "data.py").write_text(f"VALUE = {label!r}\n")
        (package / "flow.py").write_text(
            "import avalanche as ava\n"
            "from .data import VALUE\n"
            "@ava.source\ndef load():\n    return VALUE\n"
            "@ava.workflow\ndef build():\n    return load()\n"
        )
        roots.append(f"{label}={root}")
    before_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "shared" or name.startswith("shared.")
    }
    operator = Operator(roots, watch=False, schedule=False)
    try:
        workflow_ids = [item.workflow_id for item in operator.list_workflows()]
        assert workflow_ids == ["left/shared/flow.py::build", "right/shared/flow.py::build"]
        with pytest.raises(AmbiguousWorkflow):
            operator.start_run("build")
        run_ids = [operator.start_run(workflow_id) for workflow_id in workflow_ids]
        for run_id, expected in zip(run_ids, ("left", "right")):
            deadline = time.monotonic() + 10
            while operator.get_run(run_id).status not in {RunStatus.SUCCESS, RunStatus.FAILED}:
                assert time.monotonic() < deadline
                time.sleep(0.03)
            assert operator.get_run(run_id).status == RunStatus.SUCCESS
            assert operator.get_run_result(run_id) == expected
        assert {
            name: module
            for name, module in sys.modules.items()
            if name == "shared" or name.startswith("shared.")
        } == before_modules
    finally:
        operator.close()


def test_cached_catalog_recovers_from_dependency_errors_additions_and_deletions(tmp_path):
    root = tmp_path / "flows"
    root.mkdir()
    helper = root / "_schedule.py"
    helper.write_text('CRON = "1 * * * *"\n')
    flow = root / "flow.py"
    flow.write_text(
        "import avalanche as ava\nfrom _schedule import CRON\n"
        "@ava.workflow(cron=CRON)\ndef scheduled():\n    return None\n"
    )
    cache = tmp_path / "cache"
    registry = WorkflowRegistry(cache_dir=cache)
    registry.scan([str(root)])
    restarted = WorkflowRegistry(cache_dir=cache)
    restarted.scan([str(root)])
    assert restarted.resolve("scheduled").cron == "1 * * * *"

    helper.write_text("invalid Python !!!\n")
    restarted.rescan((str(helper),))
    assert restarted.resolve("scheduled").cron == "1 * * * *"
    assert [item.kind for item in restarted.list_diagnostics()] == ["import_error"]

    helper.write_text('CRON = "2 * * * *"\n')
    restarted.rescan((str(helper),))
    assert restarted.resolve("scheduled").cron == "2 * * * *"
    assert restarted.list_diagnostics() == []

    added = root / "added.py"
    added.write_text("import avalanche as ava\n@ava.workflow\ndef added():\n    return None\n")
    restarted.rescan((str(added),))
    assert {item.name for item in restarted.list_workflows()} == {"scheduled", "added"}
    flow.unlink()
    restarted.rescan((str(flow),))
    assert [item.name for item in restarted.list_workflows()] == ["added"]


def test_only_local_marked_builders_are_called(tmp_path):
    (tmp_path / "definitions.py").write_text(
        "import avalanche as ava\n" "@ava.workflow\n" "def local_flow():\n" "    return None\n"
    )
    (tmp_path / "consumer.py").write_text(
        "from definitions import local_flow\n"
        "def arbitrary_public_callable():\n"
        "    raise AssertionError('discovery called an arbitrary function')\n"
    )

    registry = WorkflowRegistry()
    registry.scan([str(tmp_path)])

    assert [item.workflow_id for item in registry.descriptors()] == [
        "definitions.py::local_flow"
    ]


def test_discovery_timeout_raises_typed_error_and_preserves_current_view(tmp_path):
    workflow_file = tmp_path / "flow.py"
    workflow_file.write_text(
        "import avalanche as ava\n" "@ava.workflow\n" "def scheduled():\n" "    return None\n"
    )
    registry = WorkflowRegistry(discovery_timeout=5.0)
    registry.scan([str(workflow_file)])
    assert registry.descriptors()

    workflow_file.write_text("while True:\n    pass\n")
    registry._discovery_timeout = 0.2
    started = time.monotonic()

    with pytest.raises(WorkflowDiscoveryTimeoutError):
        registry.rescan()

    assert time.monotonic() - started < 2.0
    assert [item.workflow_id for item in registry.descriptors()] == ["flow.py::scheduled"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
def test_successful_discovery_terminates_import_spawned_descendant(tmp_path):
    pid_file = tmp_path / "child.pid"
    workflow_file = tmp_path / "flow.py"
    workflow_file.write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "import avalanche as ava\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "@ava.workflow\n"
        "def spawned():\n"
        "    return None\n"
    )

    registry = WorkflowRegistry(discovery_timeout=10.0)
    registry.scan([str(workflow_file)])

    assert registry.resolve("spawned")
    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        os.kill(child_pid, signal.SIGKILL)
        raise AssertionError("discovery descendant remained alive")
