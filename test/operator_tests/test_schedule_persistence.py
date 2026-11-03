"""Schedule state kept in the operator state database across restarts."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from avalanche.references import local_workspace, restore_local_workspace
from runtime.operator.models import RunStatus, TriggerKind, WorkflowInfo
from runtime.operator.operator import Operator
from runtime.operator.state import OperatorStateStore, StateRowError, StateStoreError
from runtime.operator.state.schedule_input_files import (
    ScheduleInputFile,
    ScheduleInputFileFolder,
)
from runtime.operator.state.schedules import SchedulesTable, ScheduleState

_GREETING = """import avalanche as ava

class Greeting(ava.BaseInput):
    name: str
    punctuation: str = "!"

@ava.source
def greet(payload: Greeting):
    return f"hello {{payload.name}}{{payload.punctuation}}"

@ava.workflow(input=Greeting, cron="{cron}")
def greeting():
    return greet()
"""

_OTHER = """import avalanche as ava

@ava.workflow(cron="* * * * *")
def other():
    return None
"""


def _write(path: Path, *, cron: str = "* * * * *") -> None:
    path.write_text(_GREETING.format(cron=cron))


def _open(flows: Path, state_db: Path, *, schedules_enabled: bool = False) -> Operator:
    return Operator(
        [str(flows)],
        watch=False,
        schedule=False,
        webhook_port=0,
        schedules_enabled=schedules_enabled,
        state_db=state_db,
    )


def _greeting(operator: Operator) -> WorkflowInfo:
    return next(item for item in operator.list_workflows() if item.name == "greeting")


def _wait_terminal(operator: Operator, run_id: str):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = operator.get_run(run_id)
        if run.status in {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED}:
            return run
        time.sleep(0.03)
    pytest.fail(f"Run {run_id} did not finish: {operator.get_run(run_id)}")


@pytest.fixture
def flows(tmp_path):
    path = tmp_path / "flows.py"
    _write(path)
    return path


@pytest.fixture
def state_db(tmp_path):
    return tmp_path / "state" / "state.db"


def _enable_with_input(flows: Path, state_db: Path, name: str) -> None:
    operator = _open(flows, state_db)
    try:
        operator.set_schedule_enabled(_greeting(operator).selector, True, {"name": name})
    finally:
        operator.close()


def test_an_enabled_schedule_keeps_its_input_and_fires_after_a_restart(flows, state_db):
    _enable_with_input(flows, state_db, "Ada")

    operator = _open(flows, state_db)
    try:
        flow = _greeting(operator)
        assert (flow.schedule_enabled, flow.schedule_input) == (True, {"name": "Ada"})
        assert flow.next_run_at is not None
        # The first run is the occurrence after the minute it was enabled in.
        due = datetime.fromtimestamp(flow.next_run_at)
        operator._scheduler._clock = lambda: due
        operator._scheduler._check_schedules()

        (started,) = operator.list_runs(flow.selector)
        run = _wait_terminal(operator, started.run_id)
        assert (run.status, run.triggered_by) == (RunStatus.SUCCESS, TriggerKind.SCHEDULED)
        assert operator.get_run_result(run.run_id) == "hello Ada!"
    finally:
        operator.close()


@pytest.mark.parametrize("schedules_enabled", [False, True])
def test_a_schedule_turned_off_stays_off_unless_every_schedule_is_enabled(
    flows, state_db, schedules_enabled
):
    operator = _open(flows, state_db, schedules_enabled=True)
    try:
        selector = _greeting(operator).selector
        operator.set_schedule_enabled(selector, True, {"name": "Ada"})
        operator.set_schedule_enabled(selector, False)
    finally:
        operator.close()

    operator = _open(flows, state_db, schedules_enabled=schedules_enabled)
    try:
        flow = _greeting(operator)
        assert flow.schedule_enabled is schedules_enabled
        assert flow.schedule_input == {"name": "Ada"}
    finally:
        operator.close()


def test_enabling_every_schedule_for_one_start_keeps_the_stored_choices(flows, state_db):
    operator = _open(flows, state_db)
    try:
        operator.set_schedule_enabled(_greeting(operator).selector, False)
    finally:
        operator.close()
    operator = _open(flows, state_db, schedules_enabled=True)
    try:
        assert _greeting(operator).schedule_enabled is True
    finally:
        operator.close()

    operator = _open(flows, state_db)
    try:
        assert _greeting(operator).schedule_enabled is False
    finally:
        operator.close()


def test_a_schedule_whose_cron_changes_keeps_its_state(flows, state_db):
    _enable_with_input(flows, state_db, "Ada")
    # A different length, so the rewritten file never looks unchanged to discovery.
    _write(flows, cron="0 0 1 1 */2")

    operator = _open(flows, state_db)
    try:
        flow = _greeting(operator)
        assert flow.cron == "0 0 1 1 */2"
        assert (flow.schedule_enabled, flow.schedule_input) == (True, {"name": "Ada"})
    finally:
        operator.close()


def test_the_state_of_a_workflow_that_is_gone_returns_with_it(flows, state_db):
    _enable_with_input(flows, state_db, "Ada")
    flows.write_text(_OTHER)

    operator = _open(flows, state_db)
    try:
        assert [item.name for item in operator.list_workflows()] == ["other"]
    finally:
        operator.close()
    _write(flows)

    operator = _open(flows, state_db)
    try:
        flow = _greeting(operator)
        assert (flow.schedule_enabled, flow.schedule_input) == (True, {"name": "Ada"})
    finally:
        operator.close()


def test_a_restart_in_the_minute_a_schedule_fired_waits_for_its_next_occurrence(
    flows, state_db
):
    now = datetime(2026, 11, 2, 9, 0, 10)
    operator = _open(flows, state_db)
    try:
        selector = _greeting(operator).selector
        operator._scheduler._clock = lambda: now
        operator.set_schedule_enabled(selector, True, {"name": "Ada"})
        # Enabling waits for the next minute, as the next run time it reports.
        now += timedelta(minutes=1)
        operator._scheduler._check_schedules()
        (fired,) = operator.list_runs(selector)
        _wait_terminal(operator, fired.run_id)
        last_run_at = _greeting(operator).last_run_at
    finally:
        operator.close()

    operator = _open(flows, state_db)
    try:
        operator._scheduler._clock = lambda: now
        assert _greeting(operator).last_run_at == last_run_at
        operator._scheduler._check_schedules()
        assert operator._scheduler.last_triggered(_greeting(operator).workflow_id) == (
            last_run_at
        )
        now += timedelta(minutes=1)
        operator._scheduler._check_schedules()
        assert _greeting(operator).last_run_at == last_run_at + 60
    finally:
        operator.close()


def test_a_restart_in_the_minute_a_schedule_was_enabled_still_waits_for_the_next(
    flows, state_db
):
    now = datetime(2026, 11, 2, 9, 0, 10)
    operator = _open(flows, state_db)
    try:
        operator._scheduler._clock = lambda: now
        operator.set_schedule_enabled(_greeting(operator).selector, True, {"name": "Ada"})
    finally:
        operator.close()

    operator = _open(flows, state_db)
    try:
        operator._scheduler._clock = lambda: now
        operator._scheduler._check_schedules()
        assert _greeting(operator).last_run_at is None
        now += timedelta(minutes=1)
        operator._scheduler._check_schedules()
        assert _greeting(operator).last_run_at == now.replace(second=0).timestamp()
    finally:
        operator.close()


@pytest.fixture
def store():
    instance = OperatorStateStore.open(":memory:")
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [("input_json", '["not", "an", "object"]'), ("input_files_json", '{"files": [{}]}')],
)
def test_a_stored_schedule_that_does_not_match_its_model_is_an_error(
    store, tmp_path, column, value
):
    with store.transaction() as connection:
        SchedulesTable.write_enabled(
            connection, "flows.py::greeting", True, enabled_minute=None
        )
        connection.execute(f"UPDATE schedules SET {column} = ?", (value,))

    with pytest.raises(StateRowError, match=column):
        ScheduleState(store, ScheduleInputFileFolder(tmp_path, temporary=False)).load()


def test_a_missing_schedule_input_file_stops_the_operator_from_starting(store, tmp_path):
    missing = ScheduleInputFile(
        folder="0" * 32, name="main.txt", workspace_reference=f"local-ws:v1:{'1' * 32}"
    )
    with store.transaction() as connection:
        SchedulesTable.write_input(
            connection,
            "flows.py::documents",
            enabled=True,
            enabled_minute=None,
            run_input={},
            input_files=(missing,),
        )

    with pytest.raises(StateStoreError, match="main.txt"):
        ScheduleState(store, ScheduleInputFileFolder(tmp_path, temporary=False)).load()


def test_restoring_a_workspace_reference_never_moves_it_to_another_folder(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    reference = local_workspace(first).reference

    assert restore_local_workspace(reference, first).reference == reference
    with pytest.raises(ValueError, match="registered for"):
        restore_local_workspace(reference, second)
