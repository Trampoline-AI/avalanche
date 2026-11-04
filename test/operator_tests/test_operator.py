"""Core operator admission, execution, reload, and evidence contracts."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest

from runtime.operator import Operator
from runtime.operator.models import AgentEventKind, NodeState, NodeStatus, RunState, RunStatus
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


def test_spawned_run_accepts_early_event_and_typed_decision(tmp_path):
    """Use the real coordinator, pinned wait schema and synchronous control receipts."""
    import http.client
    import socket
    import subprocess
    import sys

    from runtime.operator.client import GrpcStateProvider
    from runtime.operator.server import serve
    from runtime.operator.wait_control import WaitControlRequest
    from runtime.operator.web import start_browser_server

    fixture = Path(__file__).parents[1] / "fixtures" / "durable_waits_flow.py"
    target = tmp_path / "flow.py"
    # Local operator derives the identity from its OS account, not a body field.
    import getpass

    target.write_text(
        fixture.read_text().replace(
            'approvers=["reviewer"]', f"approvers=[{getpass.getuser()!r}]"
        )
    )
    op = Operator([str(target)], watch=False, schedule=False, webhook_port=0)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = serve(op, port=port, block=False)
    address = f"127.0.0.1:{port}"
    browser = start_browser_server(address, port=0)
    client = GrpcStateProvider(address)
    try:
        (info,) = client.list_workflows()
        assert info.node_policies["draft_1"].on_uncertain == "hold"
        assert info.node_waits["decision_1"].approvers == (getpass.getuser(),)
        run_id = client.start_run(info.selector)
        event = WaitControlRequest(
            kind="event",
            address="docusign.signed",
            payload={"envelope_id": "signed"},
            idempotency_key="early",
        )
        sent = subprocess.run(
            [
                sys.executable,
                "-m",
                "ava_cli",
                "event",
                "send",
                run_id,
                "docusign.signed",
                '{"envelope_id":"signed"}',
                "--connect",
                address,
                "--idempotency-key",
                "early",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert sent.returncode == 0, sent.stderr
        receipt = client.wait_control(run_id, event).receipt
        assert receipt is not None
        assert json.loads(sent.stdout)["receipt_id"] == receipt.receipt_id
        assert receipt.run_id == run_id
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            cards = op.wait_control(run_id, WaitControlRequest(kind="list")).waits
            if any(
                card["node_id"] == "decision_1" and card["state"] == "waiting" for card in cards
            ):
                break
            time.sleep(0.03)
        else:
            pytest.fail("decision did not arm")
        http = http.client.HTTPConnection(browser.host, browser.port, timeout=10)
        http.request(
            "POST",
            f"/api/v1/runs/{run_id}/decisions/decision_1",
            body=json.dumps({"outcome": {"kind": "invalid"}, "reason": "invalid"}),
            headers={"Content-Type": "application/json", "Idempotency-Key": "invalid"},
        )
        refused = http.getresponse()
        assert refused.status == 422
        refused.read()
        http.close()
        decision = WaitControlRequest(
            kind="decision",
            address="decision_1",
            payload={"kind": "approve"},
            idempotency_key="approval",
            reason="checked pinned draft",
        )
        decided = subprocess.run(
            [
                sys.executable,
                "-m",
                "ava_cli",
                "run",
                "decide",
                run_id,
                "decision_1",
                '{"kind":"approve"}',
                "--connect",
                address,
                "--idempotency-key",
                "approval",
                "--reason",
                "checked pinned draft",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert decided.returncode == 0, decided.stderr
        audit = client.wait_control(run_id, decision).receipt
        assert audit is not None
        assert audit.principal == getpass.getuser()
        assert audit.reason == "checked pinned draft"
        assert wait_terminal(op, run_id).status == RunStatus.SUCCESS
        assert op.wait_control(run_id, event).receipt == receipt
        assert client.wait_control(run_id, decision).receipt == audit
    finally:
        client.close()
        browser.close()
        server.stop(grace=0).wait()
        op.close()


@pytest.fixture
def wait_review_operator():
    fixture = Path(__file__).parents[1] / "fixtures" / "wait_review_flows.py"
    op = Operator([str(fixture)], watch=False, schedule=False, webhook_port=0)
    try:
        yield op
    finally:
        op.close()


def wait_for_review(op, run_id):
    from avalanche.local_waits import WaitRefusalError
    from runtime.operator.wait_control import WaitControlRequest

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            cards = op.wait_control(run_id, WaitControlRequest(kind="list")).waits
        except WaitRefusalError as exc:
            # An oversized metadata response may be refused, but not break control.
            assert exc.code == "response_too_large"
            return None
        if any(card["state"] == "waiting" for card in cards):
            return cards
        time.sleep(0.03)
    pytest.fail("decision did not arm")


@pytest.mark.parametrize("read_first", [True, False])
def test_large_review_artifact_preserves_lists_and_acceptance_receipts(
    wait_review_operator, read_first
):
    import hashlib

    from runtime.operator.wait_control import WaitControlRequest

    op = wait_review_operator
    info = next(item for item in op.list_workflows() if item.name == "large_artifact")
    run_id = op.start_run(info.selector)
    if read_first:
        cards = wait_for_review(op, run_id)
        shown = cards[0]["show"]
        assert (
            shown["sha256"]
            == hashlib.sha256(b'"' + b"x" * (2 * 1024 * 1024) + b'"').hexdigest()
        )
        assert shown["truncated"] is True
    else:
        # Observe node lifecycle without listing, so receipt bounds are independent.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            node = op.get_run(run_id).nodes.get("decision_1")
            if node is not None and node.status == NodeStatus.RUNNING:
                break
            time.sleep(0.03)
        else:
            pytest.fail("decision did not start")
    decision = WaitControlRequest(
        kind="decision",
        address="decision_1",
        payload={"kind": "approve"},
        reason="checked artifact",
        idempotency_key="approval",
    )
    accepted = op.wait_control(run_id, decision)
    assert accepted.receipt is not None
    assert accepted.receipt.reviewed["truncated"] is True
    assert len(accepted.model_dump_json().encode()) < 2 * 1024 * 1024
    assert wait_terminal(op, run_id).status == RunStatus.SUCCESS
    assert op.wait_control(run_id, decision) == accepted


def test_oversized_control_response_does_not_poison_channel(wait_review_operator):
    from avalanche.local_waits import WaitRefusalError
    from runtime.operator.wait_control import WaitControlRequest

    op = wait_review_operator
    info = next(item for item in op.list_workflows() if item.name == "large_response")
    run_id = op.start_run(info.selector)
    assert wait_for_review(op, run_id) is None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        node = op.get_run(run_id).nodes.get("decision_1")
        if node is not None and node.status == NodeStatus.RUNNING:
            break
        time.sleep(0.03)
    else:
        pytest.fail("decision did not start after oversized response refusal")
    with pytest.raises(WaitRefusalError) as oversized_audit:
        op.wait_control(
            run_id,
            WaitControlRequest(
                kind="decision",
                address="decision_1",
                payload={"kind": "approve"},
                # Individually valid fields can exceed the final card's aggregate
                # budget once JSON escaping and both reviewed copies are included.
                reason="\x00" * 4096,
                idempotency_key="approval",
            ),
        )
    assert oversized_audit.value.code == "wait_card_too_large"
    assert oversized_audit.value.status == 413
    accepted = op.wait_control(
        run_id,
        WaitControlRequest(
            kind="decision",
            address="decision_1",
            payload={"kind": "approve"},
            reason="checked metadata refusal",
            idempotency_key="approval",
        ),
    )
    assert accepted.receipt is not None
    assert wait_terminal(op, run_id).status == RunStatus.SUCCESS


@pytest.mark.parametrize(
    "name", ["timeout_outcome", "timeout_failure", "pending", "unicode_approval"]
)
def test_terminal_wait_history_retains_system_audit_and_withdrawn_cards(
    wait_review_operator, name
):
    from avalanche.local_waits import WaitRefusalError
    from runtime.operator.wait_control import WaitControlRequest

    op = wait_review_operator
    workflow_name = "pending" if name == "unicode_approval" else name
    info = next(item for item in op.list_workflows() if item.name == workflow_name)
    run_id = op.start_run(info.selector)
    if name == "unicode_approval":
        assert wait_for_review(op, run_id)[0]["state"] == "waiting"
        decision = WaitControlRequest(
            kind="decision",
            address="decision_1",
            payload={"kind": "approve"},
            reason="🙂" * 3000,
            idempotency_key="unicode-approval",
        )
        accepted = op.wait_control(run_id, decision)
        assert accepted.receipt is not None
        assert accepted.receipt.reason == decision.reason
    if name == "pending":
        assert wait_for_review(op, run_id)[0]["state"] == "waiting"
        op.cancel_run(run_id)
    terminal = wait_terminal(op, run_id)
    expected = {
        "timeout_outcome": RunStatus.SUCCESS,
        "timeout_failure": RunStatus.FAILED,
        "pending": RunStatus.CANCELLED,
        "unicode_approval": RunStatus.SUCCESS,
    }
    assert terminal.status == expected[name]
    first = op.wait_control(run_id, WaitControlRequest(kind="list"))
    assert len(first.waits) == 1
    card = first.waits[0]
    if name == "unicode_approval":
        assert card["state"] == "resolved"
        assert card["audit"] == accepted.receipt.model_dump(mode="json")
        assert card["audit"]["reason"] == "🙂" * 3000
        assert op.wait_control(run_id, decision) == accepted
    elif name == "pending":
        assert card["state"] == "withdrawn"
        assert card["audit"] is None
    else:
        assert card["state"] == "resolved"
        assert card["audit"]["principal"] == "system"
        assert card["audit"]["reason"] == "decision_timeout"
        assert card["audit"]["address"] == "decision:decision_1"
    first.waits.clear()
    assert len(op.wait_control(run_id, WaitControlRequest(kind="list")).waits) == 1
    with pytest.raises(WaitRefusalError) as closed:
        op.wait_control(
            run_id,
            WaitControlRequest(
                kind="decision",
                address="decision_1",
                payload={"kind": "approve"},
                reason="late",
                idempotency_key="late",
            ),
        )
    assert closed.value.code == "run_closed"


def test_terminal_wait_pages_cross_spawn_grpc_and_rest(wait_review_operator):
    import http.client
    import socket

    from runtime.operator.client import GrpcStateProvider
    from runtime.operator.server import serve
    from runtime.operator.wait_control import WaitControlRequest
    from runtime.operator.web import start_browser_server

    op = wait_review_operator
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = serve(op, port=port, block=False)
    address = f"127.0.0.1:{port}"
    browser = start_browser_server(address, port=0)
    client = GrpcStateProvider(address)
    try:
        info = next(item for item in client.list_workflows() if item.name == "many_waits")
        run_id = client.start_run(info.selector)
        assert wait_terminal(op, run_id).status == RunStatus.SUCCESS
        first = client.wait_control(run_id, WaitControlRequest(kind="list"))
        assert len(first.waits) == 64
        assert first.next_offset == 64
        second = client.wait_control(run_id, WaitControlRequest(kind="list", offset=64))
        assert len(second.waits) == 6
        assert second.next_offset is None
        assert len({card["node_id"] for card in first.waits + second.waits}) == 70
        assert all(card["state"] == "resolved" for card in first.waits + second.waits)
        http = http.client.HTTPConnection(browser.host, browser.port, timeout=10)
        try:
            http.request("GET", f"/api/v1/runs/{run_id}/waits?offset=64&limit=2")
            response = http.getresponse()
            body = json.loads(response.read())
            assert response.status == 200
            assert len(body["waits_json"]) == 2
            assert body["next_offset"] == 66
        finally:
            http.close()
    finally:
        client.close()
        browser.close()
        server.stop(grace=0).wait()


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
        cancel_event=threading.Event(),
        result_bundle=None,
        success_quiesced=False,
        controls=None,
        cancel_writer=None,
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


@pytest.mark.parametrize("kind", get_args(AgentEventKind))
def test_every_agent_event_kind_round_trips_through_protobuf(kind):
    from runtime.operator.convert_v2 import (
        agent_event_activity_to_v2,
        agent_event_descriptor_from_v2,
    )
    from runtime.operator.models import AgentEventDescriptor
    from runtime.operator.proto import operator_pb2 as pb

    event = AgentEventDescriptor(
        invocation_id="call",
        event_sequence=2,
        size_bytes=10,
        body_token="body",
        agent_event_kind=kind,
    )
    wire = agent_event_activity_to_v2(
        event,
        run_id="run",
        node_id="agent",
        detail_ref=pb.ActivityDetailRefV2(object_key="body"),
    )
    decoded = agent_event_descriptor_from_v2(
        pb.RunActivityDescriptorV2.FromString(wire.SerializeToString())
    )
    assert decoded.agent_event_kind == kind


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
        cancel_event=threading.Event(),
        result_bundle=None,
        success_quiesced=False,
        controls=None,
        cancel_writer=None,
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
