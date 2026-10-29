import threading

import pytest
from croniter import croniter

from runtime.operator.models import (
    CatalogView,
    TriggerKind,
    WorkflowDescriptor,
    WorkflowLocator,
)
from runtime.operator.operator import Operator
from runtime.operator.scheduler import Scheduler


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
        self.started: list[tuple[str, TriggerKind]] = []
        self.inputs: list[dict | None] = []

    def start_run(
        self, workflow_id: str, triggered_by: TriggerKind, input: dict | None
    ) -> None:
        self.started.append((workflow_id, triggered_by))
        self.inputs.append(input)


def test_reconcile_is_keyed_by_canonical_id_and_fires_id():
    operator = _Operator()
    scheduler = Scheduler(operator, enabled_by_default=True)
    scheduler.reconcile(
        [_descriptor("left/flow.py::shared"), _descriptor("right/flow.py::shared")]
    )

    assert [item["workflow_id"] for item in scheduler.list_schedules()] == [
        "left/flow.py::shared",
        "right/flow.py::shared",
    ]

    scheduler._check_schedules()
    assert operator.started == [
        ("left/flow.py::shared", TriggerKind.SCHEDULED),
        ("right/flow.py::shared", TriggerKind.SCHEDULED),
    ]

    scheduler.reconcile([_descriptor("right/flow.py::shared")])
    assert [item["workflow_id"] for item in scheduler.list_schedules()] == [
        "right/flow.py::shared"
    ]


@pytest.mark.parametrize("enabled_by_default", [False, True])
def test_disabled_schedule_starts_no_runs_until_enabled(enabled_by_default):
    operator = _Operator()
    scheduler = Scheduler(operator, enabled_by_default=enabled_by_default)
    scheduler.reconcile([_descriptor("flow.py::shared")])
    scheduler.set_enabled("flow.py::shared", False)

    scheduler._check_schedules()
    assert operator.started == []
    assert scheduler.list_schedules()[0]["enabled"] is False

    scheduler.set_enabled("flow.py::shared", True)
    scheduler._check_schedules()
    assert operator.started == [("flow.py::shared", TriggerKind.SCHEDULED)]


def test_due_runs_start_with_the_stored_input():
    operator = _Operator()
    scheduler = Scheduler(operator)
    scheduler.reconcile([_descriptor("flow.py::a"), _descriptor("flow.py::b")])
    scheduler.set_enabled("flow.py::a", True, {"region": "eu"})
    scheduler.set_enabled("flow.py::b", True)

    scheduler._check_schedules()

    assert operator.inputs == [{"region": "eu"}, None]
    assert scheduler.input_for("flow.py::a") == {"region": "eu"}


def test_stored_input_is_not_shared_with_callers():
    operator = _Operator()
    scheduler = Scheduler(operator)
    scheduler.reconcile([_descriptor("flow.py::a")])
    regions = ["eu"]
    scheduler.set_enabled("flow.py::a", True, {"regions": regions})
    regions.append("us")

    scheduler._check_schedules()
    started = operator.inputs[0]
    assert started is not None
    started["regions"] = []
    read = scheduler.input_for("flow.py::a")
    assert read is not None
    read["regions"] = []

    assert scheduler.input_for("flow.py::a") == {"regions": ["eu"]}


def test_schedules_start_disabled_until_toggled():
    scheduler = Scheduler(_Operator())
    scheduler.reconcile([_descriptor("flow.py::a"), _descriptor("flow.py::b")])
    scheduler.set_enabled("flow.py::b", True)

    assert {item["workflow_id"]: item["enabled"] for item in scheduler.list_schedules()} == {
        "flow.py::a": False,
        "flow.py::b": True,
    }


def test_disable_survives_reconciliation_and_temporary_removal():
    operator = _Operator()
    scheduler = Scheduler(operator, enabled_by_default=True)
    scheduler.reconcile([_descriptor("flow.py::shared")])
    scheduler.set_enabled("flow.py::shared", False)

    scheduler.reconcile([])
    scheduler.reconcile([_descriptor("flow.py::shared", cron="*/1 * * * *")])
    scheduler._check_schedules()

    assert operator.started == []
    assert scheduler.is_enabled("flow.py::shared") is False


def test_reconcile_invalidates_descriptor_copied_before_due_claim(monkeypatch):
    operator = _Operator()
    scheduler = Scheduler(operator, enabled_by_default=True)
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
        def start_run(
            self, workflow_id: str, triggered_by: TriggerKind, input: dict | None
        ) -> None:
            entered_start.set()
            assert release_start.wait(timeout=2.0)
            super().start_run(workflow_id, triggered_by, input)

    operator = BlockingOperator()
    scheduler = Scheduler(operator, enabled_by_default=True)
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
    assert operator.started == [("flow.py::shared", TriggerKind.SCHEDULED)]
    assert scheduler.list_schedules() == []


def test_operator_refresh_serializes_catalog_publication_with_schedule_replacement():
    operator = Operator([], watch=False, schedule=False, schedules_enabled=True)
    operator._scheduler.reconcile([_descriptor("flow.py::shared")])
    catalog_published = threading.Event()
    finish_rescan = threading.Event()
    started: list[tuple[str, str]] = []

    def rescan(*, validate=None):
        catalog_published.set()
        assert finish_rescan.wait(timeout=2.0)
        descriptors = ()
        if validate is not None:
            validate(descriptors)
        return CatalogView()

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
