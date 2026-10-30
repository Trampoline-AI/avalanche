"""Enable and disable controls for discovered cron schedules."""

from __future__ import annotations

import http.client
import json
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import grpc
import pytest

from runtime.operator.client import GrpcStateProvider, OperatorCallError
from runtime.operator.convert_v2 import run_summary_from_v2
from runtime.operator.models import (
    CatalogReplaced,
    MissingRunInputError,
    RunStatus,
    TriggerKind,
    WorkflowInfo,
    missing_required_input,
)
from runtime.operator.operator import NoScheduleError, Operator
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc
from runtime.operator.server import serve as serve_operator
from runtime.operator.web import start_browser_server

_FLOWS = """import avalanche as ava

@ava.workflow(cron="{cron}")
def scheduled():
    return None

@ava.workflow
def unscheduled():
    return None

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


def _write_flows(path: Path, *, cron: str = "0 0 1 1 *") -> None:
    path.write_text(_FLOWS.format(cron=cron))


def _flow(operator: Operator, name: str) -> WorkflowInfo:
    return next(item for item in operator.list_workflows() if item.name == name)


def _wait_terminal(operator: Operator, run_id: str):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = operator.get_run(run_id)
        if run.status in {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED}:
            return run
        time.sleep(0.03)
    pytest.fail(f"Run {run_id} did not finish")


@pytest.fixture
def flows(tmp_path):
    path = tmp_path / "flows.py"
    _write_flows(path)
    return path


@pytest.fixture
def operator(flows):
    instance = Operator([str(flows)], watch=False, schedule=False, webhook_port=0)
    try:
        yield instance
    finally:
        instance.close()


def test_new_schedules_start_disabled_without_a_next_run(operator):
    flow = _flow(operator, "scheduled")

    assert flow.schedule_enabled is False
    assert flow.next_run_at is None
    assert _flow(operator, "unscheduled").schedule_enabled is False


def test_enable_and_disable_publish_the_schedule_state(operator):
    selector = _flow(operator, "scheduled").selector
    updates = operator.subscribe_operator_updates(
        operator.operator_instance_id, operator.current_sequence
    )

    enabled = operator.set_schedule_enabled(selector, True)

    assert enabled.schedule_enabled is True
    assert enabled.next_run_at is not None
    assert _flow(operator, "scheduled").schedule_enabled is True
    change = updates.get(timeout=5).update.change
    assert isinstance(change, CatalogReplaced)
    published = next(item for item in change.catalog.workflows if item.selector == selector)
    assert published.schedule_enabled is True

    disabled = operator.set_schedule_enabled(selector, False)

    assert disabled.schedule_enabled is False
    assert disabled.next_run_at is None


@pytest.mark.parametrize("enabled", [True, False])
def test_schedule_choice_survives_source_reload(flows, enabled):
    operator = Operator(
        [str(flows)],
        watch=False,
        schedule=False,
        webhook_port=0,
        schedules_enabled=not enabled,
    )
    try:
        selector = _flow(operator, "scheduled").selector
        revision = operator.get_catalog().revision
        operator.set_schedule_enabled(selector, enabled)

        _write_flows(flows, cron="*/5 * * * *")
        operator._refresh_workflows()

        flow = _flow(operator, "scheduled")
        assert operator.get_catalog().revision > revision
        assert flow.cron == "*/5 * * * *"
        assert flow.schedule_enabled is enabled
        assert (flow.next_run_at is not None) is enabled
    finally:
        operator.close()


def test_toggling_does_not_change_the_workflow_version(operator):
    flow = _flow(operator, "scheduled")

    enabled = operator.set_schedule_enabled(flow.selector, True)

    assert enabled.version == flow.version


def test_operator_can_start_with_every_schedule_enabled(flows):
    operator = Operator(
        [str(flows)], watch=False, schedule=False, webhook_port=0, schedules_enabled=True
    )
    try:
        flow = _flow(operator, "scheduled")
        assert flow.schedule_enabled is True
        assert flow.next_run_at is not None
        assert not operator.set_schedule_enabled(flow.selector, False).schedule_enabled
    finally:
        operator.close()


def test_schedule_control_rejects_workflows_without_cron(operator):
    selector = _flow(operator, "unscheduled").selector

    with pytest.raises(NoScheduleError):
        operator.set_schedule_enabled(selector, True)


def test_scheduled_runs_start_with_the_schedule_input(flows):
    _write_flows(flows, cron="* * * * *")
    operator = Operator([str(flows)], watch=False, schedule=False, webhook_port=0)
    try:
        selector = _flow(operator, "greeting").selector
        now = datetime(2026, 10, 29, 9, 0, 40)
        operator._scheduler._clock = lambda: now
        enabled = operator.set_schedule_enabled(selector, True, {"name": "ops"})
        assert enabled.schedule_input == {"name": "ops"}

        now += timedelta(minutes=1)
        operator._scheduler._check_schedules()

        (started,) = operator.list_runs(selector)
        run = _wait_terminal(operator, started.run_id)
        assert run.status is RunStatus.SUCCESS
        assert run.triggered_by is TriggerKind.SCHEDULED
        assert operator.get_run_result(run.run_id) == "hello ops!"
    finally:
        operator.close()


def test_schedule_input_is_kept_until_replaced(operator):
    flow = _flow(operator, "greeting")
    assert flow.schedule_input is None
    operator.set_schedule_enabled(flow.selector, True, {"name": "first"})

    disabled = operator.set_schedule_enabled(flow.selector, False)
    reenabled = operator.set_schedule_enabled(flow.selector, True)
    replaced = operator.set_schedule_enabled(flow.selector, True, {"name": "second"})

    assert disabled.schedule_input == {"name": "first"}
    assert reenabled.schedule_input == {"name": "first"}
    assert replaced.schedule_input == {"name": "second"}
    assert replaced.version == flow.version


def test_schedule_input_is_only_accepted_when_enabling(operator):
    selector = _flow(operator, "greeting").selector

    with pytest.raises(ValueError):
        operator.set_schedule_enabled(selector, False, {"name": "ops"})
    assert _flow(operator, "greeting").schedule_input is None


def test_operator_rejects_non_finite_schedule_input_from_python_callers(operator):
    selector = _flow(operator, "greeting").selector

    with pytest.raises(ValueError):
        operator.set_schedule_enabled(selector, True, {"name": ["ops", float("inf")]})

    flow = _flow(operator, "greeting")
    assert (flow.schedule_enabled, flow.schedule_input) == (False, None)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def served(operator, tmp_path):
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "index.html").write_text("<main>Avalanche</main>")
    port = _free_port()
    grpc_server = serve_operator(operator, port=port, block=False)
    browser = start_browser_server(f"127.0.0.1:{port}", port=0, asset_root=tmp_path / "public")
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield operator, browser, pb_grpc.OperatorServiceV2Stub(channel)
    finally:
        channel.close()
        browser.close()
        grpc_server.stop(grace=0).wait()


def _rest(server, method: str, path: str, body: dict | None = None):
    connection = http.client.HTTPConnection(server.host, server.port, timeout=10)
    try:
        encoded = None if body is None else json.dumps(body).encode()
        connection.request(
            method, path, body=encoded, headers={"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _flow_path(selector: str) -> str:
    return f"/api/v1/flows/{quote(selector, safe='')}/schedule"


def test_rest_enables_and_disables_a_schedule(served):
    operator, browser, _ = served
    selector = _flow(operator, "scheduled").selector

    status, flow = _rest(browser, "POST", _flow_path(selector), {"enabled": True})
    assert status == 200
    assert flow["schedule_enabled"] is True
    assert flow["next_run_at"] > 0

    status, page = _rest(browser, "GET", "/api/v1/flows")
    listed = next(item for item in page["flows"] if item["workflow_selector"] == selector)
    assert listed["schedule_enabled"] is True

    status, flow = _rest(browser, "POST", _flow_path(selector), {"enabled": False})
    assert status == 200
    assert flow["schedule_enabled"] is False
    assert flow["next_run_at"] == 0


def test_rest_enables_a_schedule_with_input(served):
    operator, browser, _ = served
    selector = _flow(operator, "greeting").selector

    status, flow = _rest(
        browser, "POST", _flow_path(selector), {"enabled": True, "input_json": {"name": "ops"}}
    )

    assert status == 200
    assert json.loads(flow["schedule_input_json"]) == {"name": "ops"}
    assert _flow(operator, "greeting").schedule_input == {"name": "ops"}


@pytest.mark.parametrize(
    ("name", "body", "status"),
    [
        ("unscheduled", {"enabled": False}, 409),
        ("missing", {"enabled": False}, 404),
        ("scheduled", {"enabled": "no"}, 400),
        ("scheduled", {}, 400),
        ("greeting", {"enabled": True, "input_json": ["ops"]}, 400),
        ("greeting", {"enabled": False, "input_json": {"name": "ops"}}, 400),
        ("greeting", {"enabled": True, "input_json": {"name": float("nan")}}, 400),
    ],
)
def test_rest_schedule_route_rejects_invalid_targets_and_bodies(served, name, body, status):
    operator, browser, _ = served
    selector = next(
        (item.selector for item in operator.list_workflows() if item.name == name),
        "missing.py::missing",
    )

    actual, payload = _rest(browser, "POST", _flow_path(selector), body)

    assert actual == status
    assert set(payload) == {"error"}


def test_api_starts_are_api_triggers_unless_marked_manual(served):
    operator, browser, stub = served
    selector = _flow(operator, "unscheduled").selector

    status, started = _rest(
        browser, "POST", "/api/v1/runs", {"workflow_selector": selector, "run_id": "rest"}
    )
    assert status == 202
    stub.StartRun(
        pb.StartRunRequestV2(
            run_id="ui",
            workflow_selector=selector,
            input_json="{}",
            context_json="{}",
            trigger=pb.START_RUN_TRIGGER_V2_MANUAL,
        )
    )
    for run_id in ("rest", "ui"):
        _wait_terminal(operator, run_id)

    assert operator.get_run("rest").triggered_by is TriggerKind.API
    assert operator.get_run("ui").triggered_by is TriggerKind.MANUAL
    with pytest.raises(grpc.RpcError) as error:
        stub.StartRun(
            pb.StartRunRequestV2(
                run_id="bad",
                workflow_selector=selector,
                input_json="{}",
                context_json="{}",
                trigger=7,
            )
        )
    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT


@pytest.fixture
def provider(operator):
    port = _free_port()
    grpc_server = serve_operator(operator, port=port, block=False)
    client = GrpcStateProvider(f"127.0.0.1:{port}")
    try:
        yield client
    finally:
        client.close()
        grpc_server.stop(grace=0).wait()


def test_grpc_client_enables_and_disables_a_schedule_with_input(operator, provider):
    selector = _flow(operator, "greeting").selector

    enabled = provider.set_schedule_enabled(selector, True, {"name": "ops"})
    assert (enabled.schedule_enabled, enabled.schedule_input) == (True, {"name": "ops"})
    assert enabled.next_run_at

    disabled = provider.set_schedule_enabled(selector, False)
    assert (disabled.schedule_enabled, disabled.schedule_input) == (False, {"name": "ops"})
    assert disabled.next_run_at is None

    again = provider.set_schedule_enabled(selector, True)
    assert again.schedule_input == {"name": "ops"}


@pytest.mark.parametrize(
    ("name", "enabled", "run_input", "status"),
    [
        ("unscheduled", True, None, grpc.StatusCode.FAILED_PRECONDITION),
        ("missing", True, None, grpc.StatusCode.NOT_FOUND),
        ("greeting", False, {"name": "ops"}, grpc.StatusCode.INVALID_ARGUMENT),
    ],
)
def test_grpc_schedule_control_rejects_invalid_requests(
    operator, provider, name, enabled, run_input, status
):
    selector = next(
        (item.selector for item in operator.list_workflows() if item.name == name),
        "missing.py::missing",
    )

    with pytest.raises(OperatorCallError) as error:
        provider.set_schedule_enabled(selector, enabled, run_input)

    assert error.value.status is status


def test_clients_reject_unknown_trigger_kinds():
    with pytest.raises(ValueError):
        run_summary_from_v2(pb.RunSummaryV2(run_id="run-1", triggered_by="timer"))


@pytest.mark.parametrize(
    ("payload", "enabled"),
    [
        ('{"name": NaN}', True),
        ('{"name": ["ops", Infinity]}', True),
        ("null", True),
        ("null", False),
    ],
)
def test_grpc_rejects_invalid_schedule_input_without_storing_it(served, payload, enabled):
    operator, _, stub = served
    selector = _flow(operator, "greeting").selector

    with pytest.raises(grpc.RpcError) as error:
        stub.SetScheduleEnabled(
            pb.SetScheduleEnabledRequestV2(
                workflow_selector=selector, enabled=enabled, input_json=payload
            )
        )

    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    flow = _flow(operator, "greeting")
    assert (flow.schedule_enabled, flow.schedule_input) == (False, None)
    assert stub.DiscoverFlows(pb.DiscoverFlowsRequestV2()).flows


def test_a_schedule_cannot_be_enabled_without_its_required_input(operator):
    selector = _flow(operator, "greeting").selector

    with pytest.raises(MissingRunInputError) as error:
        operator.set_schedule_enabled(selector, True, {"punctuation": "?"})
    assert error.value.fields == ("name",)
    with pytest.raises(MissingRunInputError):
        operator.set_schedule_enabled(selector, True)
    assert _flow(operator, "greeting").schedule_enabled is False

    operator.set_schedule_enabled(selector, True, {"name": "ops"})
    operator.set_schedule_enabled(selector, False)
    # The stored input still carries the required field.
    assert operator.set_schedule_enabled(selector, True).schedule_enabled is True


@pytest.mark.parametrize("trigger", [TriggerKind.MANUAL, TriggerKind.API])
def test_a_run_without_its_required_input_is_refused_before_it_starts(operator, trigger):
    selector = _flow(operator, "greeting").selector

    with pytest.raises(MissingRunInputError):
        operator.start_run(selector, trigger, run_id="missing", input={"punctuation": "?"})

    assert operator.list_runs(selector) == []


@pytest.mark.parametrize("trigger", [TriggerKind.SCHEDULED, TriggerKind.WEBHOOK])
def test_cron_and_webhook_runs_without_required_input_still_start_and_fail(operator, trigger):
    selector = _flow(operator, "greeting").selector

    run_id = operator.start_run(selector, trigger, run_id="unattended", input={})

    assert _wait_terminal(operator, run_id).status is RunStatus.FAILED


def test_rest_refuses_runs_and_schedules_without_required_input(served):
    operator, browser, _ = served
    selector = _flow(operator, "greeting").selector

    enable_status, enable_error = _rest(
        browser, "POST", _flow_path(selector), {"enabled": True}
    )
    start_status, start_error = _rest(
        browser,
        "POST",
        "/api/v1/runs",
        {"workflow_selector": selector, "run_id": "missing", "input_json": {}},
    )

    assert (enable_status, start_status) == (400, 400)
    assert enable_error["error"]["code"] == start_error["error"]["code"] == "INVALID_ARGUMENT"
    assert operator.list_runs(selector) == []


def test_grpc_refuses_runs_and_schedules_without_required_input(operator, provider):
    selector = _flow(operator, "greeting").selector

    with pytest.raises(OperatorCallError) as start:
        provider.start_run(selector, run_id="missing", input={})
    with pytest.raises(OperatorCallError) as enable:
        provider.set_schedule_enabled(selector, True)

    assert start.value.status is grpc.StatusCode.INVALID_ARGUMENT
    assert enable.value.status is grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    ("field", "rejects_null"),
    [
        ({"type": "string"}, True),
        ({"type": ["integer", "null"]}, False),
        ({"anyOf": [{"type": "string"}, {"type": "null"}]}, False),
        ({"oneOf": [{"type": "string"}, {"type": "integer"}]}, True),
        ({"enum": ["a", "b"]}, True),
        ({"const": None}, False),
        # Any / JsonValue: no constraint, so null is allowed.
        ({}, False),
        ({"$ref": "#/$defs/Region"}, True),
        ({"$ref": "#/$defs/Anything"}, False),
        ({"allOf": [{"$ref": "#/$defs/Region"}]}, True),
    ],
)
def test_null_counts_as_missing_only_when_the_field_rejects_null(field, rejects_null):
    schema = json.dumps(
        {
            "properties": {"value": field},
            "required": ["value"],
            "$defs": {"Region": {"enum": ["eu", "us"]}, "Anything": {}},
        }
    )

    assert missing_required_input(schema, {"value": None}) == (
        ("value",) if rejects_null else ()
    )
    assert missing_required_input(schema, None) == ("value",)
    assert missing_required_input("", None) == ()
