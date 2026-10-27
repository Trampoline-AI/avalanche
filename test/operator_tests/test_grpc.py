"""Core client/server behavior over real gRPC, plus reset ownership races."""

import hashlib
import json
import os
import socket
import threading
import time
from types import SimpleNamespace

import grpc
import pytest
from pydantic import ValidationError

from avalanche.runtime import File
from runtime.operator import Operator
from runtime.operator.client import (
    GrpcStateProvider,
    OperatorCallError,
    StaleResetAcknowledgementError,
    StreamState,
    _DetailHydrationRaceError,
)
from runtime.operator.models import (
    CatalogSnapshot,
    NodeState,
    ResetBaseline,
    ResetBaselineCursor,
    RunState,
    RunStatus,
)
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.server import serve


def _unused_port():
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        return sock.getsockname()[1]


def _event_ulid(sequence):
    return f"{sequence:026X}"


def _cursor(sequence):
    return pb.LifecycleCursorV2(
        stream="operator-events",
        topology_fingerprint="operator-events-topology",
        stream_generation=1,
        retained_floor_event_ulid=_event_ulid(0),
        event_ulid=_event_ulid(sequence),
    )


def _baseline_cursor(sequence):
    return ResetBaselineCursor(
        stream="operator-events",
        topology_fingerprint="operator-events-topology",
        stream_generation=1,
        retained_floor_event_ulid=_event_ulid(0),
        event_ulid=_event_ulid(sequence),
    )


def _run_created_envelope(sequence, run_id, *, instance, status="pending"):
    return pb.RunStatusEnvelopeV2(
        scope_ref=pb.ScopeReferenceV2(reference=instance),
        event_ulid=_event_ulid(sequence),
        cursor=_cursor(sequence),
        run_created=pb.RunCreatedV2(
            summary=pb.RunSummaryV2(
                run_id=run_id,
                workflow_selector="flow",
                workflow_display_name="flow",
                status=status,
                created_sequence=sequence,
                revision=sequence,
            ),
        ),
    )


def _event_handle():
    return SimpleNamespace(
        cancel_event=threading.Event(), result_bundle=None, success_quiesced=False
    )


def _wait_for_terminal(client, run_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = client.get_run(run_id)
        if run and run.status in (RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED):
            return run
        time.sleep(0.02)
    pytest.fail(f"Run {run_id} did not finish")


@pytest.fixture
def client(tmp_path):
    workflow_path = tmp_path / "transport.py"
    workflow_path.write_text(
        """import time
import avalanche as ava

class Input(ava.BaseInput):
    message: str
    document: ava.File

class Context(ava.RunContext):
    request_id: str

@ava.source
def echo(payload: Input, ctx: Context):
    return {
        "message": payload.message,
        "document": payload.document,
        "request_id": ctx.request_id,
        "run_id": ctx.run_id,
        "lineage": ctx.lineage_vector,
    }

@ava.workflow(input=Input, context=Context)
def transport():
    return echo()

@ava.source
def wait():
    time.sleep(30)

@ava.workflow
def slow():
    return wait()
"""
    )
    operator = Operator([str(workflow_path)], schedule=False, watch=False)
    server = serve(operator, port=(port := _unused_port()), block=False)
    provider = GrpcStateProvider(f"localhost:{port}")
    try:
        yield provider
    finally:
        provider.close()
        server.stop(grace=0).wait()
        operator.close()


def test_discovery_start_stream_and_large_result_preserve_input_and_context(
    client, monkeypatch
):
    flows = {flow.name: flow for flow in client.list_workflows()}
    assert set(flows) == {"transport", "slow"}
    assert not os.path.isabs(flows["transport"].file_path)
    updates = []
    finished = threading.Event()

    def on_run(run):
        updates.append((run.run_id, run.status))
        if run.run_id == "run-transport" and run.status is RunStatus.SUCCESS:
            finished.set()

    def recover(notice):
        baseline = client.load_reset_baseline(notice)
        client.acknowledge_stream_reset(
            baseline.generation, baseline.operator_instance_id, baseline.as_of_event_ulid
        )

    client.on_run_update(on_run)
    client.on_stream_reset(recover)
    client.start_stream()
    deadline = time.monotonic() + 5
    while client.stream_state is not StreamState.LIVE and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client.stream_state is StreamState.LIVE
    assert updates == []

    content = b"x" * (4 * 1024 * 1024 + 1)
    selector = flows["transport"].selector
    assert (
        client.start_run(
            selector,
            run_id="run-transport",
            input={"message": "delivered"},
            context={
                "request_id": "request-1",
                "run_id": "forged",
                "lineage_vector": {"x": "y"},
            },
            files={"document": File(name="large.bin", content=content)},
        )
        == "run-transport"
    )
    assert finished.wait(15)
    run = _wait_for_terminal(client, "run-transport")
    assert run.status is RunStatus.SUCCESS
    statuses = [status for run_id, status in updates if run_id == run.run_id]
    assert statuses.index(RunStatus.REQUESTING) < statuses.index(RunStatus.RUNNING)
    assert statuses.index(RunStatus.RUNNING) < statuses.index(RunStatus.SUCCESS)
    result = client.get_run_result(run.run_id)
    assert result.pop("document").content == content
    assert result == {
        "message": "delivered",
        "request_id": "request-1",
        "run_id": run.run_id,
        "lineage": {},
    }
    assert [item.run_id for item in client.list_runs(selector)] == [run.run_id]
    with pytest.raises(OperatorCallError) as error:
        client.start_run(selector, run_id=run.run_id)
    assert error.value.status is grpc.StatusCode.ALREADY_EXISTS

    read_result = client._stub.GetRunResult

    def corrupt_result(request, **kwargs):
        response = read_result(request, **kwargs)
        response.value.sha256 = "0" * 64
        return response

    monkeypatch.setattr(client._stub, "GetRunResult", corrupt_result)
    with pytest.raises(ValueError):
        client.get_run_result(run.run_id)


def test_cancelled_run_never_exposes_a_success_result(client):
    run_id = client.start_run("slow")
    client.cancel_run(run_id)
    assert _wait_for_terminal(client, run_id).status is RunStatus.CANCELLED
    with pytest.raises(grpc.RpcError) as error:
        client.get_run_result(run_id)
    assert error.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert client.get_run("missing") is None


def test_canonical_and_ambiguous_grpc_selection(tmp_path):
    roots = [tmp_path / "left", tmp_path / "right"]
    source = "import avalanche as ava\n" "@ava.workflow\n" "def shared():\n" "    return None\n"
    for root in roots:
        root.mkdir()
        (root / "flow.py").write_text(source)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    operator = Operator(
        workflow_paths=[str(root) for root in roots], schedule=False, watch=False
    )
    server = serve(operator, port=port, block=False)
    provider = GrpcStateProvider(f"localhost:{port}")
    try:
        workflows = provider.list_workflows()
        assert [item.display_name for item in workflows] == ["shared", "shared"]
        ids = [item.workflow_id for item in workflows]
        assert ids == ["left/flow.py::shared", "right/flow.py::shared"]
        assert all(not os.path.isabs(item.file_path) for item in workflows)

        run_id = provider.start_run(ids[0])
        assert [run.run_id for run in provider.list_runs(ids[0])] == [run_id]
        assert provider.list_runs(ids[1]) == []

        with pytest.raises(OperatorCallError) as error:
            provider.start_run("shared")
        assert error.value.status is grpc.StatusCode.INVALID_ARGUMENT
        assert ids[0] in str(error.value)
        assert ids[1] in str(error.value)

        with pytest.raises(OperatorCallError) as error:
            provider.list_runs("shared")
        assert error.value.status is grpc.StatusCode.INVALID_ARGUMENT

        (roots[0] / "flow.py").write_text("VALUE = 1\n")
        failed_run_id = provider.start_run(ids[0])
        failed_run = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            failed_run = provider.get_run(failed_run_id)
            if failed_run is not None and failed_run.status == RunStatus.FAILED:
                break
            time.sleep(0.05)
        assert failed_run is not None
        assert failed_run.status == RunStatus.FAILED
    finally:
        provider.close()
        server.stop(grace=1)
        operator.close()


def _replay(trace, *, invocation_id: str, run_id: str):
    """The evidence a PredictRLM step sends for ``trace``, made by the real adapter."""
    from predict_rlm import RunEvent, RunEventKind, RunEvidence

    from avalanche.runtimes.predict_rlm.replay import replay

    events = [
        RunEvent(run_id, index, RunEventKind.ITERATION_RECORDED, index, {"step": step})
        for index, step in enumerate(trace.steps, start=1)
    ]
    evidence = RunEvidence(run_id=run_id, complete=True, terminal_outcome="completed")
    return replay(events, evidence, invocation_id=invocation_id, trace=trace)


def _apply_agent_evidence(operator, run_id: str, evidence) -> None:
    operator._apply_event(
        run_id,
        _event_handle(),
        {"type": "agent_evidence", "node_id": "agent-1", "event": evidence.model_dump_json()},
    )


def _seed_hydration_run(operator, run_id: str, agent_trace, iteration_step) -> RunState:
    run = RunState(run_id=run_id, flow_name="flow")
    run.nodes["agent-1"] = NodeState(
        node_id="agent-1",
        name="Agent",
        node_type="step",
    )
    operator._runs[run_id] = run
    operator._notify_run(run)
    steps = [iteration_step(sequence, f"print({sequence})") for sequence in range(1, 6)]
    trace = agent_trace(steps=steps, telemetry_ref={"trace_id": "trace-1"})
    for evidence in _replay(trace, invocation_id="test-invocation", run_id=f"sdk-{run_id}"):
        _apply_agent_evidence(operator, run_id, evidence)
    return run


def test_paged_details_and_chunked_trace_materialize_without_cross_run_invalidation(
    monkeypatch, agent_trace, iteration_step
):
    monkeypatch.setattr("runtime.operator.client.DETAIL_HYDRATION_PAGE_SIZE", 2)
    # Small chunks make the final body cross several transport chunks.
    monkeypatch.setattr("runtime.operator.server_v2.TRACE_CHUNK_BYTES", 256)
    operator = Operator([], watch=False, schedule=False)
    run = _seed_hydration_run(operator, "run-target", agent_trace, iteration_step)
    unrelated = RunState(run_id="run-unrelated", flow_name="flow")
    operator._runs[unrelated.run_id] = unrelated
    operator._notify_run(unrelated)
    server = serve(operator, port=(port := _unused_port()), block=False)
    provider = GrpcStateProvider(f"localhost:{port}")
    delegate = provider._stub

    class UpdatingStub:
        def __getattr__(self, name):
            return getattr(delegate, name)

        def ListRunActivity(self, request, **kwargs):  # noqa: N802
            response = delegate.ListRunActivity(request, **kwargs)
            unrelated.status = RunStatus.RUNNING
            operator._notify_run(unrelated)
            return response

    provider._stub = UpdatingStub()
    try:
        hydrated = provider.get_run(run.run_id)
        assert hydrated.details_hydrated
        envelope = json.loads(hydrated.nodes["agent-1"].agent_trace_json)
        assert [event["sequence"] for event in envelope["events"]] == [1, 2, 3, 4, 5]
        assert envelope["invocation_id"] is None
        assert envelope["terminal"] is None
        assert len(hydrated.logs) == 6
        trace = provider.hydrate_trace(run.run_id, "agent-1")
        body = trace.trace_body
        assert body.invocation_id == "test-invocation"
        assert len(body.model_dump_json().encode()) > 3 * 256
        assert body.record.header.native.telemetry_ref == {"trace_id": "trace-1"}
        assert body.record.turns == ()
        assert body.record.header.turn_count == 5
        assert body.record.header.external_run_id == "sdk-run-target"
        assert body.complete is True
        assert provider.hydrate_trace(run.run_id, "agent-1").trace_body == body
        hydrated.logs.clear()
        envelope["events"].clear()
        again = provider.get_run(run.run_id)
        assert len(again.logs) == 6
        again_envelope = json.loads(again.nodes["agent-1"].agent_trace_json)
        assert len(again_envelope["events"]) == 5
        assert again_envelope["terminal"] == body.model_dump(mode="json")
        assert again_envelope["invocation_id"] == "test-invocation"
    finally:
        provider.close()
        server.stop(grace=0).wait()
        operator.close()


def test_grpc_terminal_hydration_selects_body_invocation_across_event_pages(
    monkeypatch, agent_trace, iteration_step
):
    monkeypatch.setattr("runtime.operator.client.DETAIL_HYDRATION_PAGE_SIZE", 1)
    operator = Operator([], watch=False, schedule=False)
    run = RunState(run_id="run-multiple-invocations", flow_name="flow")
    run.nodes["agent-1"] = NodeState("agent-1", "Agent", "step")
    operator._runs[run.run_id] = run
    operator._notify_run(run)
    from predict_rlm.trace import PredictCallDetail, PredictCallGroup, ToolCall

    def with_calls(step, tools, predicts):
        step.tool_calls = [ToolCall(name=f"tool_{i}", duration_ms=1) for i in range(tools)]
        step.predict_calls = [
            PredictCallGroup(
                signature="q -> a",
                model="sub",
                calls=[PredictCallDetail(duration_ms=1) for _ in range(predicts)],
            )
        ]
        return step

    step_a = with_calls(iteration_step(1, "call_a()"), 2, 3)
    step_b = with_calls(iteration_step(1, "call_b()"), 7, 11)
    sent_a = _replay(agent_trace(steps=[step_a]), invocation_id="call-a", run_id="sdk-call-a")
    sent_b = _replay(agent_trace(steps=[step_b]), invocation_id="call-b", run_id="sdk-call-b")
    # Both invocations stream their turn; only call-a has finished.
    for evidence in (sent_a[0], sent_b[0], sent_a[1]):
        _apply_agent_evidence(operator, run.run_id, evidence)
    server = serve(operator, port=(port := _unused_port()), block=False)
    provider = GrpcStateProvider(f"localhost:{port}")
    try:
        live = provider.get_run(run.run_id)
        envelope = json.loads(live.nodes["agent-1"].agent_trace_json)
        assert envelope["invocation_id"] is None
        assert [event["invocation_id"] for event in envelope["events"]] == [
            "call-a",
            "call-b",
        ]
        detail = provider.hydrate_trace(run.run_id, "agent-1")
        assert detail.trace_body.invocation_id == "call-a"
        assert detail.trace_body.record.header.external_run_id == "sdk-call-a"
        hydrated = provider.get_run(run.run_id)
        envelope = json.loads(hydrated.nodes["agent-1"].agent_trace_json)
        assert envelope["invocation_id"] == "call-a"
        # Each turn keeps its tool calls, predict calls included.
        assert [
            (event["invocation_id"], len(event["common"]["turn"]["tool_calls"]))
            for event in envelope["events"]
        ] == [("call-a", 5), ("call-b", 18)]
        in_process = operator.get_run(run.run_id)
        local_envelope = json.loads(in_process.nodes["agent-1"].agent_trace_json)
        assert local_envelope["invocation_id"] == "call-a"
        assert local_envelope["terminal"] == envelope["terminal"]
        # The operator shows only the finished invocation's turns next to its trace.
        assert [event["invocation_id"] for event in local_envelope["events"]] == ["call-a"]
        retained = operator.read_trace(
            run.run_id, "agent-1", operator_instance_id=operator.operator_instance_id
        )
        assert json.loads(retained.data)["invocation_id"] == "call-a"
        assert hydrated.nodes["agent-1"].trace.size_bytes == len(retained.data)
    finally:
        provider.close()
        server.stop(grace=0).wait()
        operator.close()


def test_grpc_discards_hydration_after_epoch_reset(monkeypatch, agent_trace, iteration_step):
    monkeypatch.setattr(
        "runtime.operator.client.DETAIL_HYDRATION_PAGE_SIZE",
        2,
    )
    operator = Operator([], watch=False, schedule=False)
    server = None
    provider = None
    release = threading.Event()
    page_read = threading.Event()
    results = []
    errors = []
    try:
        run = _seed_hydration_run(operator, "run-stale-hydration", agent_trace, iteration_step)
        port = _unused_port()
        server = serve(operator, port=port, block=False)
        provider = GrpcStateProvider(f"localhost:{port}")
        delegate = provider._stub

        class BlockingStub:
            def __getattr__(self, name):
                return getattr(delegate, name)

            def ListRunActivity(self, request, **kwargs):  # noqa: N802
                response = delegate.ListRunActivity(request, **kwargs)
                if not page_read.is_set():
                    page_read.set()
                    if not release.wait(2):
                        raise RuntimeError("hydration barrier timed out")
                return response

        provider._stub = BlockingStub()

        def read_run():
            try:
                results.append(provider.get_run(run.run_id))
            except Exception as error:
                errors.append(error)

        reader = threading.Thread(target=read_run)
        reader.start()
        assert page_read.wait(2)
        provider._install_structural_baseline("replacement-epoch", _event_ulid(0), {})
        release.set()
        reader.join(2)
        assert not reader.is_alive()
        assert results == []
        assert len(errors) == 1
        assert isinstance(errors[0], _DetailHydrationRaceError)
        assert provider._cursor.operator_instance_id == "replacement-epoch"
        assert provider._runs_by_id == {}
    finally:
        release.set()
        if provider is not None:
            provider.close()
        if server is not None:
            server.stop(grace=1)
        operator.close()


def test_restart_reset_rejects_stale_generation_and_rebinds_update_epoch():
    def load_baseline(notice):
        return ResetBaseline(
            generation=notice.generation,
            operator_instance_id="operator-restarted",
            as_of_event_ulid=_event_ulid(3),
            catalog=CatalogSnapshot(workflows=()),
            runs_by_workflow={},
            cursor=_baseline_cursor(3),
        )

    provider = GrpcStateProvider(
        "localhost:1",
        reset_baseline_loader=load_baseline,
    )
    reset_notices = []
    reset_observed = threading.Event()
    waiting = threading.Event()
    release = threading.Event()
    received = []

    reset_envelope = _run_created_envelope(
        2, "run_recovered", instance="operator-restarted", status="success"
    )

    class RestartedStream:
        def initial_metadata(self):
            return ()

        def __iter__(self):
            yield _run_created_envelope(4, "run_newer", instance="operator-restarted")
            waiting.set()
            release.wait()

    class RestartedStub:
        def __init__(self):
            self.calls = 0

        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            self.calls += 1
            if self.calls == 1:
                assert request.after_cursor.event_ulid == _event_ulid(99)
                return iter((reset_envelope,))
            assert request.after_cursor.event_ulid == _event_ulid(3)
            return RestartedStream()

    def on_reset(notice):
        reset_notices.append(notice)
        reset_observed.set()

    provider._stub = RestartedStub()
    provider._install_structural_baseline("operator-original", _event_ulid(99), {})
    provider._event_cursor = _cursor(99)
    provider.on_stream_reset(on_reset)
    provider._run_callbacks.append(lambda run: received.append(run.run_id))
    try:
        provider._ensure_stream()
        assert reset_observed.wait(timeout=1.0)
        notice = reset_notices[0]
        assert provider.stream_state is StreamState.RESET_REQUIRED

        baseline = provider.load_reset_baseline(notice)
        assert baseline.operator_instance_id == "operator-restarted"
        assert baseline.as_of_event_ulid == _event_ulid(3)
        with pytest.raises(StaleResetAcknowledgementError):
            provider.acknowledge_stream_reset(
                generation=notice.generation + 1,
                operator_instance_id="operator-restarted",
                reconciled_event_ulid=_event_ulid(3),
            )
        provider.acknowledge_stream_reset(
            generation=notice.generation,
            reconciled_event_ulid=_event_ulid(3),
            operator_instance_id="operator-restarted",
        )
        assert waiting.wait(timeout=1.0)
        assert provider.stream_state is StreamState.LIVE
        assert provider._cursor.operator_instance_id == "operator-restarted"
        assert provider._cursor.event_ulid == _event_ulid(4)
        assert received == ["run_newer"]
    finally:
        provider._stream_stop.set()
        release.set()
        provider.close()


def test_concurrent_close_and_stream_start_leave_no_live_thread_or_calls():
    provider = GrpcStateProvider("localhost:1")
    participant_count = 16
    barrier = threading.Barrier(participant_count + 2)
    close_returned = threading.Event()

    class Unavailable(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.UNAVAILABLE

        def details(self):
            return "offline"

    class FailingStub:
        def __init__(self):
            self.calls = 0
            self.post_close_calls = 0

        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            self.calls += 1
            if close_returned.is_set():
                self.post_close_calls += 1
            raise Unavailable()

    stub = FailingStub()
    provider._stub = stub

    def register():
        barrier.wait()
        provider.on_run_update(lambda _run: None)
        provider.start_stream()

    def close():
        barrier.wait()
        provider.close()
        close_returned.set()

    registrations = [threading.Thread(target=register) for _ in range(participant_count)]
    closer = threading.Thread(target=close)
    for registration in registrations:
        registration.start()
    closer.start()
    barrier.wait()
    for registration in registrations:
        registration.join()
    closer.join()

    thread = provider._stream_thread
    assert thread is None or not thread.is_alive()
    assert stub.calls <= 1
    assert stub.post_close_calls == 0


@pytest.mark.parametrize(("status", "complete"), [("failed", True), ("cancelled", False)])
def test_grpc_hydrates_pre_trace_terminal_evidence(status, complete):
    from avalanche.agent.live import AgentTraceUnavailable

    operator = Operator([], watch=False, schedule=False)
    run = RunState(run_id="run-pre-trace", flow_name="flow")
    run.nodes["agent-1"] = NodeState("agent-1", "Agent", "step")
    operator._runs[run.run_id] = run
    operator._notify_run(run)
    unavailable = AgentTraceUnavailable(
        invocation_id="pre-trace-invocation",
        runtime_kind="predict_rlm",
        status=status,
        error="Stopped before the first iteration",
        external_run_id="sdk-pre-trace",
        complete=complete,
    )
    _apply_agent_evidence(operator, run.run_id, unavailable)
    server = serve(operator, port=(port := _unused_port()), block=False)
    provider = GrpcStateProvider(f"localhost:{port}")
    try:
        detail = provider.hydrate_trace(run.run_id, "agent-1")
        assert detail is not None
        assert detail.trace_body == unavailable
        hydrated = provider.get_run(run.run_id)
        assert hydrated.nodes["agent-1"].trace.complete is complete
        envelope = json.loads(hydrated.nodes["agent-1"].agent_trace_json)
        assert envelope["invocation_id"] == "pre-trace-invocation"
        assert envelope["status"] == "unavailable"
        assert envelope["terminal"] == unavailable.model_dump(mode="json")
        assert provider.hydrate_trace(run.run_id, "agent-1").trace_body == unavailable
    finally:
        provider.close()
        server.stop(grace=0).wait()
        operator.close()


@pytest.mark.parametrize(
    "body",
    [
        # A v1 body is no longer accepted.
        b'{"invocation_id":"test-invocation","trace":null,"evidence":null}',
        b'{"kind":"trace_unavailable","invocation_id":"test-invocation"}',
        b'{"kind":"trace_finished","schema_version":2,"invocation_id":"test-invocation",'
        b'"record":{"header":{}},"complete":true}',
        b'{"kind":"trace_finished","schema_version":1}',
        b'{"trace":',
    ],
)
def test_corrupt_terminal_body_is_not_treated_as_a_hydration_race(
    body, agent_trace, iteration_step
):
    operator = Operator([], watch=False, schedule=False)
    run = _seed_hydration_run(operator, "run-corrupt", agent_trace, iteration_step)
    server = serve(operator, port=(port := _unused_port()), block=False)
    provider = GrpcStateProvider(f"localhost:{port}")
    delegate = provider._stub

    class CorruptingStub:
        def __getattr__(self, name):
            return getattr(delegate, name)

        def GetRunSnapshot(self, request, **kwargs):  # noqa: N802
            response = delegate.GetRunSnapshot(request, **kwargs)
            trace = response.nodes[0].trace
            trace.size_bytes = len(body)
            trace.detail_ref.size_bytes = len(body)
            trace.detail_ref.sha256 = hashlib.sha256(body).hexdigest()
            return response

        def ReadActivityDetail(self, request, **kwargs):  # noqa: N802
            if request.detail_ref.activity_id.startswith("trace:"):
                return iter([pb.ActivityDetailChunkV2(chunk_index=0, data=body, eof=True)])
            return delegate.ReadActivityDetail(request, **kwargs)

    provider._stub = CorruptingStub()
    try:
        with pytest.raises(ValidationError):
            provider.hydrate_trace(run.run_id, "agent-1")
        assert (run.run_id, "agent-1") not in provider._trace_bodies
    finally:
        provider.close()
        server.stop(grace=0).wait()
        operator.close()
