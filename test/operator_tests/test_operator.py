"""Core operator admission, execution, reload, and evidence contracts."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.operator import Operator
from runtime.operator.models import NodeState, NodeStatus, RunState, RunStatus
from runtime.operator.operator import RunAlreadyExistsError
from runtime.operator.webhooks import WebhookRoute


@pytest.fixture
def operator():
    fixtures = Path(__file__).parents[1] / "fixtures" / "sample_workflows.py"
    instance = Operator([str(fixtures)], watch=False, schedule=False)
    try:
        yield instance
    finally:
        instance.close()


def wait_terminal(operator, run_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = operator.get_run(run_id)
        if run.status in {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED}:
            return run
        time.sleep(0.03)
    pytest.fail(f"Run {run_id} did not finish")


def test_discovered_workflow_executes_nodes_and_returns_detached_state(operator):
    workflow = next(
        item for item in operator.list_workflows() if item.name == "simple_workflow"
    )
    transitions = []
    operator.on_run_update(
        lambda run: transitions.extend(
            (node.node_id, node.status) for node in run.nodes.values()
        )
    )
    run_id = operator.start_run(workflow.workflow_id)
    run = wait_terminal(operator, run_id)
    assert run.status == RunStatus.SUCCESS
    assert {node.status for node in run.nodes.values()} == {NodeStatus.SUCCESS}
    for node_id in run.nodes:
        states = [status for observed, status in transitions if observed == node_id]
        assert states.index(NodeStatus.RUNNING) < states.index(NodeStatus.SUCCESS)
    run.status = RunStatus.FAILED
    run.logs.clear()
    assert operator.get_run(run_id).status == RunStatus.SUCCESS
    assert operator.get_run(run_id).logs


def test_run_id_reservation_is_atomic(operator):
    barrier = threading.Barrier(2)

    def reserve():
        barrier.wait(timeout=5)
        try:
            return operator.start_run("simple_workflow", run_id="run_concurrent")
        except RunAlreadyExistsError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert results.count("run_concurrent") == 1
    assert sum(isinstance(result, RunAlreadyExistsError) for result in results) == 1
    assert wait_terminal(operator, "run_concurrent").status == RunStatus.SUCCESS


def test_input_files_and_context_cannot_spoof_operator_run_identity(operator):
    run_id = operator.start_run(
        "input_workflow",
        input={"message": "hello", "document": {"name": "note.txt", "content": b"contents"}},
        context={"request_id": "req_456", "run_id": "spoofed_user_id"},
    )
    run = wait_terminal(operator, run_id)
    assert run.status == RunStatus.SUCCESS
    messages = "\n".join(entry.message for entry in run.logs)
    assert "message=hello" in messages
    assert "request_id=req_456" in messages
    assert f"run_id={run_id}" in messages
    assert "run_id=spoofed_user_id" not in messages
    assert "file=contents" in messages


def test_start_run_publishes_multiple_requesting_runs_before_preparation(operator, monkeypatch):
    op = operator
    release_preparation = threading.Event()
    await_prepared = op._await_prepared

    def delay_preparation(handle):
        assert release_preparation.wait(timeout=5)
        return await_prepared(handle)

    monkeypatch.setattr(op, "_await_prepared", delay_preparation)
    try:
        first_run_id = op.start_run("simple_workflow")
        second_run_id = op.start_run("simple_workflow")

        assert op.get_run(first_run_id).status == RunStatus.REQUESTING
        assert op.get_run(second_run_id).status == RunStatus.REQUESTING

        release_preparation.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            statuses = [
                op.get_run(first_run_id).status,
                op.get_run(second_run_id).status,
            ]
            if statuses == [RunStatus.SUCCESS, RunStatus.SUCCESS]:
                break
            time.sleep(0.05)

        assert statuses == [RunStatus.SUCCESS, RunStatus.SUCCESS]
    finally:
        release_preparation.set()
        op.close()


def test_refresh_reconciliation_failure_rolls_back_and_can_retry(
    tmp_path,
    monkeypatch,
):
    workflow_file = tmp_path / "flow.py"
    workflow_file.write_text(
        "import avalanche as ava\n" "@ava.workflow\n" "def flow():\n" "    return None\n"
    )
    operator = Operator(
        workflow_paths=[str(workflow_file)],
        webhook_port=0,
        schedule=False,
        watch=False,
    )
    previous = operator._registry.view
    real_reconcile = operator._webhooks.reconcile
    reject_candidate = True

    def reconcile(routes: dict[str, WebhookRoute]) -> None:
        nonlocal reject_candidate
        if routes and reject_candidate:
            reject_candidate = False
            raise OSError("port occupied")
        real_reconcile(routes)

    monkeypatch.setattr(operator._webhooks, "reconcile", reconcile)
    try:
        workflow_file.write_text(
            "import avalanche as ava\n"
            "@ava.workflow(webhook=True)\n"
            "def flow():\n"
            "    return None\n"
        )
        with pytest.raises(RuntimeError, match="Workflow reload reconciliation failed"):
            operator._refresh_workflows()

        assert operator._registry.view is previous

        workflow_file.write_text(
            "import avalanche as ava\n"
            "@ava.workflow(cron='5 * * * *', webhook=True)\n"
            "def flow():\n"
            "    return None\n"
        )
        operator._refresh_workflows()

        assert operator.get_catalog().revision == previous.revision + 1
    finally:
        operator.close()


def test_agent_trace_header_and_events_carry_common_fields_over_protobuf(
    agent_trace, iteration_step
):
    from avalanche.agent.trace import TokenUsage
    from runtime.operator.convert_v2 import (
        agent_event_activity_to_v2,
        agent_event_descriptor_from_v2,
        trace_header_from_v2,
        trace_header_to_v2,
    )
    from runtime.operator.proto import operator_pb2 as pb

    operator = Operator([], watch=False, schedule=False)
    run = RunState(run_id="run-agent", flow_name="agent-flow", status=RunStatus.RUNNING)
    run.nodes["agent_1"] = NodeState("agent_1", "agent", "step", status=NodeStatus.RUNNING)
    operator._runs[run.run_id] = run
    handle = SimpleNamespace(
        cancel_event=threading.Event(), result_bundle=None, success_quiesced=False
    )

    def apply(event):
        operator._apply_event(
            run.run_id, handle, {"type": "agent_evidence", "node_id": "agent_1", "event": event}
        )

    from predict_rlm import RunEvent, RunEventKind, RunEvidence
    from predict_rlm.trace import PredictCallDetail, PredictCallGroup, ToolCall

    from avalanche.runtimes.predict_rlm.replay import replay

    step = iteration_step(1, "x = 1")
    step.tool_calls = [
        ToolCall(name="lookup", duration_ms=1),
        ToolCall(name="fetch", duration_ms=1),
    ]
    step.predict_calls = [
        PredictCallGroup(
            signature="q -> a", model="sub", calls=[PredictCallDetail(duration_ms=1)]
        )
    ]
    sent = replay(
        [
            RunEvent("sdk-run", 1, RunEventKind.RUN_STARTED, 1, {"inputs": {}}),
            RunEvent("sdk-run", 2, RunEventKind.ITERATION_RECORDED, 2, {"step": step}),
        ],
        RunEvidence(run_id="sdk-run", complete=True, terminal_outcome="completed"),
        invocation_id="inv",
        trace=agent_trace(steps=[step]),
    )
    try:
        for event in sent:
            apply(event.model_dump_json())
        summaries = operator.list_run_summaries()
        snapshot = operator.get_run_snapshot(
            run.run_id,
            operator_instance_id=summaries.operator_instance_id,
            as_of_sequence=summaries.as_of_sequence,
        )
        header = snapshot.nodes[0].trace.header
        assert (header.status, header.runtime_kind, header.trace_format) == (
            "succeeded",
            "predict_rlm",
            "predict_rlm.run/v1",
        )
        assert (header.model, header.turn_count, header.stop_reason) == (
            "test-model",
            1,
            "submit",
        )
        assert header.usage == TokenUsage(input_tokens=0, output_tokens=0, cost_usd=0.0)
        # Two tool calls and one predict call.
        assert header.tool_call_count == 3
        assert header.external_run_id == "sdk-run"
        # PredictRLM's own header part travels with the summary.
        assert header.native.max_iterations == 10
        wire_header = pb.TraceHeaderV2.FromString(
            trace_header_to_v2(header).SerializeToString()
        )
        assert trace_header_from_v2(wire_header) == header
        # A runtime part of another format, or none at all, is rejected on arrival.
        mismatched = pb.TraceHeaderV2()
        mismatched.CopyFrom(wire_header)
        mismatched.trace_format = "claude_agent.messages/v1"
        missing = pb.TraceHeaderV2()
        missing.CopyFrom(wire_header)
        missing.native_json = ""
        for invalid in (mismatched, missing):
            with pytest.raises(ValueError):
                trace_header_from_v2(invalid)

        page = operator.list_agent_events(page_token=snapshot.nodes[0].event_page_token)
        setup, turn = page.events
        # Neither event has a PredictRLM part: both are common events alone.
        assert (setup.native_event_kind, setup.agent_event_kind, setup.turn) == (
            "",
            "run.started",
            None,
        )
        assert (turn.native_event_kind, turn.agent_event_kind, turn.turn) == (
            "",
            "turn.finished",
            1,
        )
        assert turn.tool_call_count == 3
        wire = agent_event_activity_to_v2(
            turn, run_id=run.run_id, node_id="agent_1", detail_ref=pb.ActivityDetailRefV2()
        )
        decoded = agent_event_descriptor_from_v2(
            pb.RunActivityDescriptorV2.FromString(wire.SerializeToString())
        )
        assert (decoded.agent_event_kind, decoded.turn, decoded.tool_call_count) == (
            "turn.finished",
            1,
            3,
        )
        started = agent_event_descriptor_from_v2(
            pb.RunActivityDescriptorV2.FromString(
                agent_event_activity_to_v2(
                    setup,
                    run_id=run.run_id,
                    node_id="agent_1",
                    detail_ref=pb.ActivityDetailRefV2(),
                ).SerializeToString()
            )
        )
        assert (started.native_event_kind, started.agent_event_kind) == ("", "run.started")
    finally:
        operator.close()


@pytest.mark.parametrize(
    "field, value",
    [("status", 0), ("status", 99), ("runtime_kind", "")],
)
def test_trace_header_from_protobuf_rejects_unknown_status_and_missing_runtime(field, value):
    from runtime.operator.convert_v2 import trace_header_from_v2
    from runtime.operator.proto import operator_pb2 as pb

    message = pb.TraceHeaderV2(
        status=pb.AGENT_TRACE_STATUS_V2_SUCCEEDED,
        runtime_kind="predict_rlm",
        trace_format="predict_rlm.run/v1",
    )
    setattr(message, field, value)
    with pytest.raises(ValueError):
        trace_header_from_v2(message)


def test_agent_evidence_deduplicates_per_invocation_and_materializes_references(
    agent_trace, iteration_step, agent_evidence
):
    operator = Operator([], watch=False, schedule=False)
    run = RunState(run_id="run-agent", flow_name="agent-flow", status=RunStatus.RUNNING)
    run.nodes["agent_1"] = NodeState("agent_1", "agent", "step", status=NodeStatus.RUNNING)
    operator._runs[run.run_id] = run
    handle = SimpleNamespace(
        cancel_event=threading.Event(), result_bundle=None, success_quiesced=False
    )

    def apply(event):
        operator._apply_event(
            run.run_id, handle, {"type": "agent_evidence", "node_id": "agent_1", "event": event}
        )

    def code(event):
        return event["common"]["turn"]["native"]["code"]

    try:
        for invocation, output in (("first", "one"), ("second", "two")):
            step = iteration_step(1, output)
            live, terminal = agent_evidence(
                agent_trace(steps=[step]), invocation_id=invocation, run_id=f"sdk-{invocation}"
            )
            apply(live.model_dump_json())
            apply(live.model_dump_json())
            if invocation == "first":
                for live_run in (
                    operator.get_run(run.run_id),
                    operator.list_runs("")[0],
                ):
                    live_trace = json.loads(live_run.nodes["agent_1"].agent_trace_json)
                    assert [code(item) for item in live_trace["events"]] == ["one"]
                    assert live_trace["terminal"] is None
            apply(terminal.model_dump_json())
        summaries = operator.list_run_summaries()
        snapshot = operator.get_run_snapshot(
            run.run_id,
            operator_instance_id=summaries.operator_instance_id,
            as_of_sequence=summaries.as_of_sequence,
        )
        page = operator.list_agent_events(page_token=snapshot.nodes[0].event_page_token)
        assert [(event.invocation_id, event.event_sequence) for event in page.events] == [
            ("first", 1),
            ("second", 2),
        ]
        bodies = [json.loads(operator.read_detail(event.body_token)) for event in page.events]
        assert [code(body) for body in bodies] == ["one", "two"]
        materialized = operator.get_run(run.run_id)
        trace = json.loads(materialized.nodes["agent_1"].agent_trace_json)
        assert trace["status"] == "succeeded"
        assert trace["invocation_id"] == "second"
        assert [code(event) for event in trace["events"]] == ["two"]
        assert trace["terminal"]["header"]["external_run_id"] == "sdk-second"
        assert trace["terminal"]["complete"] is True
        retained = json.loads(
            operator.read_trace(
                run.run_id, "agent_1", operator_instance_id=operator.operator_instance_id
            ).data
        )
        assert retained["invocation_id"] == "second"
        # Turns travelled as live events; the final body carries the header alone.
        assert "turns" not in retained and "record" not in retained
        assert retained["header"]["external_run_id"] == "sdk-second"
    finally:
        operator.close()
