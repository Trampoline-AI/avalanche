from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

import avalanche as ava
from avalanche.operator import Operator
from avalanche.operator.models import RunStatus
from avalanche.storage import Namespace, NamespaceConfig, ScanResult, Table


class RecordingTable(Table):
    schema: Any = None

    def __init__(self) -> None:
        super().__init__()
        self.bound = False

    @property
    def current_version_id(self) -> int | None:
        return None

    def append(self, df: Any) -> Any:
        raise NotImplementedError

    def scan(self, *args: Any, **kwargs: Any) -> ScanResult:
        raise NotImplementedError


def make_namespace(
    tmp_path: Path,
    events: list[str],
    *,
    failure: Exception | None = None,
    provision_delay: float = 0.0,
) -> Namespace:
    state_lock = threading.Lock()

    class RecordingNamespace(Namespace):
        ns_config = NamespaceConfig(
            name="managed",
            base_location=str(tmp_path),
        )
        records = RecordingTable()

        def __init__(self) -> None:
            self.active_pushes = 0
            self.max_active_pushes = 0
            self.push_count = 0
            super().__init__()

        def push(self) -> None:
            with state_lock:
                self.active_pushes += 1
                self.max_active_pushes = max(
                    self.max_active_pushes,
                    self.active_pushes,
                )
                self.push_count += 1
            try:
                events.append("push")
                if provision_delay:
                    time.sleep(provision_delay)
                if failure is not None:
                    raise failure
                Path(self.location).mkdir(parents=True, exist_ok=True)
                self.records.bound = True
            finally:
                with state_lock:
                    self.active_pushes -= 1

        def drop(self, *, drop_tables: bool = False) -> None:
            return None

    return RecordingNamespace()


def test_declaration_records_without_mutation_and_provisions_before_table_binding(
    tmp_path: Path,
):
    events: list[str] = []
    namespace = make_namespace(tmp_path, events)

    @ava.source
    def read_bound_table(records=namespace.records):
        events.append("node")
        return records.bound, records._ns is namespace, Path(records.location).parent.is_dir()

    @ava.workflow(namespaces=[namespace, namespace])
    def flow():
        return read_bound_table()

    assert events == []
    built = flow()
    assert built.namespaces == (namespace,)
    assert events == []
    assert not Path(namespace.location).exists()

    assert built.run(executor=ava.LocalExecutor()).result(timeout=5) == (
        True,
        True,
        True,
    )
    assert events == ["push", "node"]


def test_provisioning_failure_fails_run_before_nodes_start(tmp_path: Path):
    events: list[str] = []
    namespace = make_namespace(
        tmp_path,
        events,
        failure=ValueError("catalog unavailable"),
    )

    @ava.source
    def unreachable():
        events.append("node")

    @ava.workflow(namespaces=[namespace])
    def flow():
        return unreachable()

    handle = flow().run(executor=ava.LocalExecutor())

    with pytest.raises(
        RuntimeError,
        match=(
            "Failed to provision namespace 'managed' for workflow 'flow': "
            "catalog unavailable"
        ),
    ) as raised:
        handle.result(timeout=5)

    assert isinstance(raised.value.__cause__, ValueError)
    assert events == ["push"]


def test_concurrent_starts_serialize_idempotent_provisioning(tmp_path: Path):
    events: list[str] = []
    namespace = make_namespace(tmp_path, events, provision_delay=0.02)

    @ava.source
    def observe_resource():
        return Path(namespace.location).is_dir()

    @ava.workflow(namespaces=[namespace])
    def flow():
        return observe_resource()

    handles = [flow().run(executor=ava.LocalExecutor()) for _ in range(8)]

    assert [handle.result(timeout=5) for handle in handles] == [True] * 8
    assert namespace.push_count == 8
    assert namespace.max_active_pushes == 1
    assert Path(namespace.location).is_dir()


def test_workflow_without_declarations_preserves_explicit_push(tmp_path: Path):
    events: list[str] = []
    namespace = make_namespace(tmp_path, events)
    namespace.push()

    @ava.source
    def observe_resource():
        events.append("node")
        return Path(namespace.location).is_dir()

    @ava.workflow
    def flow():
        return observe_resource()

    built = flow()
    assert built.namespaces == ()
    assert built.run(executor=ava.LocalExecutor()).result(timeout=5) is True
    assert events == ["push", "node"]
    assert namespace.push_count == 1


def test_operator_discovery_is_read_only_and_triggered_run_provisions(tmp_path: Path):
    base_location = tmp_path / "catalog"
    namespace_location = base_location / "operator-managed"
    workflow_file = tmp_path / "managed_workflow.py"
    workflow_file.write_text(
        "from pathlib import Path\n"
        "import avalanche as ava\n"
        "from avalanche.storage import Namespace, NamespaceConfig\n"
        "class ManagedNamespace(Namespace):\n"
        "    ns_config = NamespaceConfig(\n"
        "        name='operator-managed',\n"
        f"        base_location={str(base_location)!r},\n"
        "    )\n"
        "    def push(self):\n"
        "        Path(self.location).mkdir(parents=True, exist_ok=True)\n"
        "    def drop(self, *, drop_tables=False):\n"
        "        return None\n"
        "ns = ManagedNamespace()\n"
        "@ava.source\n"
        "def verify():\n"
        "    assert Path(ns.location).is_dir()\n"
        "    return True\n"
        "@ava.workflow(namespaces=[ns])\n"
        "def managed_workflow():\n"
        "    return verify()\n"
    )

    operator = Operator(
        workflow_paths=[str(workflow_file)],
        schedule=False,
        watch=False,
    )
    try:
        assert [item.name for item in operator.list_workflows()] == ["managed_workflow"]
        assert not namespace_location.exists()

        run_id = operator.start_run("managed_workflow")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = operator.get_run(run_id)
            if run is not None and run.status in (RunStatus.SUCCESS, RunStatus.FAILED):
                break
            time.sleep(0.05)

        run = operator.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.SUCCESS, [entry.message for entry in run.logs]
        assert namespace_location.is_dir()
    finally:
        operator.close()
