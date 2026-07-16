import threading
from types import SimpleNamespace

from croniter import croniter

from avalanche.operator.models import WorkflowDescriptor, WorkflowLocator
from avalanche.operator.scheduler import Scheduler
from runtime.operator.operator import Operator


def _descriptor(workflow_id: str, cron: str | None = "* * * * *") -> WorkflowDescriptor:
    return WorkflowDescriptor(
        workflow_id=workflow_id,
        display_name="shared",
        locator=WorkflowLocator("root", "flow.py", "shared"),
        node_ids=(),
        graph=(),
        node_types=(),
        display_names=(),
        cron=cron,
    )


class _Operator:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []

    def start_run(self, workflow_id: str, triggered_by: str) -> None:
        self.started.append((workflow_id, triggered_by))


def test_reconcile_is_keyed_by_canonical_id_and_fires_id():
    operator = _Operator()
    scheduler = Scheduler(operator)
    scheduler.reconcile(
        [_descriptor("left/flow.py::shared"), _descriptor("right/flow.py::shared")]
    )

    assert [item["workflow_id"] for item in scheduler.list_schedules()] == [
        "left/flow.py::shared",
        "right/flow.py::shared",
    ]

    scheduler._check_schedules()
    assert operator.started == [
        ("left/flow.py::shared", "scheduled"),
        ("right/flow.py::shared", "scheduled"),
    ]

    scheduler.reconcile([_descriptor("right/flow.py::shared")])
    assert [item["workflow_id"] for item in scheduler.list_schedules()] == [
        "right/flow.py::shared"
    ]


def test_reconcile_invalidates_descriptor_copied_before_due_claim(monkeypatch):
    operator = _Operator()
    scheduler = Scheduler(operator)
    old = _descriptor("flow.py::shared")
    scheduler.reconcile([old])
    entered = threading.Event()
    resume = threading.Event()
    original_match = croniter.match

    def blocked_match(expression, moment):
        entered.set()
        assert resume.wait(timeout=2.0)
        return original_match(expression, moment)

    monkeypatch.setattr("runtime.operator.scheduler.croniter.match", blocked_match)
    checker = threading.Thread(target=scheduler._check_schedules)
    checker.start()
    assert entered.wait(timeout=2.0)
    scheduler.reconcile([])
    resume.set()
    checker.join(timeout=2.0)

    assert not checker.is_alive()
    assert operator.started == []


def test_reconcile_waits_for_claimed_dispatch_to_start_before_replacing_registration():
    entered_start = threading.Event()
    release_start = threading.Event()
    reconciled = threading.Event()

    class BlockingOperator(_Operator):
        def start_run(self, workflow_id: str, triggered_by: str) -> None:
            entered_start.set()
            assert release_start.wait(timeout=2.0)
            super().start_run(workflow_id, triggered_by)

    operator = BlockingOperator()
    scheduler = Scheduler(operator)
    scheduler.reconcile([_descriptor("flow.py::shared")])
    checker = threading.Thread(target=scheduler._check_schedules)
    checker.start()
    assert entered_start.wait(timeout=2.0)

    def reconcile_removed():
        scheduler.reconcile([])
        reconciled.set()

    reconciler = threading.Thread(target=reconcile_removed)
    reconciler.start()
    assert not reconciled.wait(timeout=0.1)
    release_start.set()
    checker.join(timeout=2.0)
    reconciler.join(timeout=2.0)

    assert not checker.is_alive()
    assert not reconciler.is_alive()
    assert operator.started == [("flow.py::shared", "scheduled")]
    assert scheduler.list_schedules() == []


def test_operator_refresh_serializes_catalog_publication_with_schedule_replacement():
    operator = Operator([], watch=False, schedule=False)
    operator._scheduler.reconcile([_descriptor("flow.py::shared")])
    catalog_published = threading.Event()
    finish_rescan = threading.Event()
    started: list[tuple[str, str]] = []

    def rescan():
        catalog_published.set()
        assert finish_rescan.wait(timeout=2.0)
        return SimpleNamespace(by_id={})

    operator._registry.rescan = rescan
    operator.start_run = lambda workflow_id, triggered_by: started.append(
        (workflow_id, triggered_by)
    )

    refresher = threading.Thread(target=operator._refresh_workflows)
    refresher.start()
    assert catalog_published.wait(timeout=2.0)

    checker = threading.Thread(target=operator._scheduler._check_schedules)
    checker.start()
    assert checker.is_alive()

    finish_rescan.set()
    refresher.join(timeout=2.0)
    checker.join(timeout=2.0)
    operator.close()

    assert not refresher.is_alive()
    assert not checker.is_alive()
    assert started == []
