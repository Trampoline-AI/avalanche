"""Wire-level pagination, replay recovery, and opaque reference safety."""

import socket
import threading
import time
from dataclasses import replace
from datetime import datetime

import grpc
import pytest

from avalanche import source, workflow
from avalanche.runtime import File
from runtime.operator.client import GrpcStateProvider, StreamState
from runtime.operator.models import LogEntry, LogLevel, RunState, RunStatus, SequencedLogEntry
from runtime.operator.operator import Operator
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc
from runtime.operator.result_store import publish_workflow_result
from runtime.operator.results import encode_workflow_result
from runtime.operator.server import serve


def _unused_port():
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        return sock.getsockname()[1]


@pytest.fixture
def transport():
    operator = Operator([], schedule=False, watch=False, stream_history_capacity=2)
    port = _unused_port()
    server = serve(operator, port=port, block=False)
    channel = grpc.insecure_channel(f"localhost:{port}")
    try:
        grpc.channel_ready_future(channel).result(timeout=5)
        yield operator, pb_grpc.OperatorServiceV2Stub(channel), f"localhost:{port}"
    finally:
        channel.close()
        server.stop(grace=0).wait()
        operator.close()


def _seed_run_with_logs(operator: Operator, run_id: str, messages: tuple[str, ...]) -> RunState:
    run = RunState(
        run_id=run_id,
        flow_name="flow",
        status=RunStatus.SUCCESS,
        workflow_id="flow.py::flow",
        workflow_display_name="flow",
    )
    entries = [
        SequencedLogEntry(
            sequence=index,
            entry=LogEntry(
                timestamp=datetime(2026, 8, 17, 12, 0, index),
                level=LogLevel.INFO,
                node_id="node",
                message=message,
            ),
            size_bytes=len(message.encode("utf-8")),
        )
        for index, message in enumerate(messages, start=1)
    ]
    run.latest_log_sequence = len(entries)
    with operator._lock:
        operator._runs[run_id] = run
        operator._logs[run_id] = entries
    operator._notify_run(run)
    return run


def _seed_result_file(operator: Operator, run_id: str) -> None:
    pending = operator._result_store.prepare()
    try:
        digest = publish_workflow_result(
            encode_workflow_result(File(name="result.txt", content=b"result file")),
            pending.descriptor,
            (pending.device, pending.inode),
            threading.Event(),
        )
        stored = operator._result_store.accept(pending, digest)
    except (OSError, RuntimeError, TypeError, ValueError):
        operator._result_store.discard(pending)
        raise
    with operator._lock:
        operator._stored_results[run_id] = stored


def test_catalog_and_run_pagination_keep_their_original_snapshot(transport):
    operator, service, _ = transport

    @source
    def value():
        return "value"

    @workflow
    def alpha():
        return value()

    @workflow
    def beta():
        return value()

    @workflow
    def omega():
        return value()

    operator._registry.register(alpha)
    operator._registry.register(omega)
    first = service.DiscoverFlows(pb.DiscoverFlowsRequestV2(page_size=1))
    operator._registry.register(beta)
    operator._publish_catalog(operator._registry.view)
    second = service.DiscoverFlows(
        pb.DiscoverFlowsRequestV2(page_size=1, continuation=first.next_page)
    )
    assert [flow.workflow_selector for flow in [*first.flows, *second.flows]] == [
        "alpha",
        "omega",
    ]
    assert [
        flow.workflow_selector
        for flow in service.DiscoverFlows(pb.DiscoverFlowsRequestV2()).flows
    ] == ["alpha", "beta", "omega"]

    for run_id in ("run-1", "run-2", "run-3"):
        _seed_run_with_logs(operator, run_id, ())
    first = service.ListRunSummaries(pb.ListRunSummariesRequestV2(page_size=2))
    _seed_run_with_logs(operator, "run-new", ())
    second = service.ListRunSummaries(
        pb.ListRunSummariesRequestV2(page_size=2, continuation=first.next_page)
    )
    assert [run.run_id for run in first.runs] == ["run-3", "run-2"]
    assert [run.run_id for run in second.runs] == ["run-1"]
    assert not second.next_page.continuation_id


def test_activity_pages_are_exclusive_and_bound_to_run_and_category(transport):
    operator, service, _ = transport
    _seed_run_with_logs(operator, "run-a", ("a-one", "a-two", "a-three"))
    _seed_run_with_logs(operator, "run-b", ("b-one",))
    first = service.ListRunActivity(pb.ListRunActivityRequestV2(run_id="run-a", page_size=2))
    second = service.ListRunActivity(
        pb.ListRunActivityRequestV2(run_id="run-a", page_size=2, continuation=first.next_page)
    )
    bodies = [
        b"".join(
            chunk.data
            for chunk in service.ReadActivityDetail(
                pb.ReadActivityDetailRequestV2(detail_ref=item.detail_ref)
            )
        )
        for item in [*first.activities, *second.activities]
    ]
    assert bodies == [b"a-one", b"a-two", b"a-three"]
    assert not second.next_page.continuation_id
    for run_id, node_id in (("run-b", ""), ("run-a", "node")):
        with pytest.raises(grpc.RpcError) as error:
            service.ListRunActivity(
                pb.ListRunActivityRequestV2(
                    run_id=run_id, node_id=node_id, continuation=first.next_page
                )
            )
        assert error.value.code() is grpc.StatusCode.FAILED_PRECONDITION


def test_activity_detail_refs_are_immutable_and_rederived_before_streaming(transport):
    operator, service, _ = transport
    _seed_run_with_logs(operator, "run-detail", ("original body",))
    page = service.ListRunActivity(
        pb.ListRunActivityRequestV2(run_id="run-detail", page_size=10)
    )
    reference = page.activities[0].detail_ref

    body = b"".join(
        chunk.data
        for chunk in service.ReadActivityDetail(
            pb.ReadActivityDetailRequestV2(detail_ref=reference)
        )
    )
    assert body == b"original body"

    forged = pb.ActivityDetailRefV2()
    forged.CopyFrom(reference)
    forged.sha256 = "0" * 64
    with pytest.raises(grpc.RpcError) as forged_error:
        list(service.ReadActivityDetail(pb.ReadActivityDetailRequestV2(detail_ref=forged)))
    assert forged_error.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    with operator._lock:
        original = operator._logs["run-detail"][0]
        operator._logs["run-detail"][0] = replace(
            original,
            entry=replace(original.entry, message="rewritten body with a different size"),
        )

    with pytest.raises(grpc.RpcError) as stale_error:
        list(service.ReadActivityDetail(pb.ReadActivityDetailRequestV2(detail_ref=reference)))
    assert stale_error.value.code() == grpc.StatusCode.DATA_LOSS


def test_artifact_refs_are_immutable_and_reject_forged_bindings(transport):
    operator, service, _ = transport
    _seed_run_with_logs(operator, "run-artifact", ())
    _seed_result_file(operator, "run-artifact")

    result = service.GetRunResult(pb.GetRunResultRequestV2(run_id="run-artifact"))
    reference = result.files[0].artifact_ref

    body = b"".join(
        chunk.data
        for chunk in service.ReadRunOutputArtifact(
            pb.ReadRunOutputArtifactRequestV2(artifact_ref=reference)
        )
    )
    assert body == b"result file"

    forged = pb.RunOutputArtifactRefV2()
    forged.CopyFrom(reference)
    forged.size_bytes += 1
    with pytest.raises(grpc.RpcError) as excinfo:
        list(
            service.ReadRunOutputArtifact(
                pb.ReadRunOutputArtifactRequestV2(artifact_ref=forged)
            )
        )
    assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_retained_replay_then_gap_reset_recovers_orphan_runs_and_live_updates(transport):
    operator, service, address = transport

    def create(run_id):
        run = RunState(run_id=run_id, flow_name="removed-flow")
        operator._runs[run_id] = run
        operator._notify_run(run)

    create("run-a")
    issued = service.ListRunSummaries(pb.ListRunSummariesRequestV2()).cursor
    create("run-b")
    replay = service.WatchRunStatus(pb.WatchRunStatusRequestV2(after_cursor=issued), timeout=5)
    try:
        assert next(replay).run_created.summary.run_id == "run-b"
    finally:
        replay.cancel()
    for run_id in ("run-c", "run-d"):
        create(run_id)
    expired = service.WatchRunStatus(pb.WatchRunStatusRequestV2(after_cursor=issued), timeout=5)
    try:
        assert next(expired).HasField("reset_required")
    finally:
        expired.cancel()

    provider = GrpcStateProvider(address)
    baselines = []
    live_update = threading.Event()

    def recover(notice):
        baseline = provider.load_reset_baseline(notice)
        baselines.append(baseline)
        provider.acknowledge_stream_reset(
            baseline.generation, baseline.operator_instance_id, baseline.as_of_event_ulid
        )

    provider._install_structural_baseline(operator.operator_instance_id, issued.event_ulid, {})
    provider._event_cursor = issued
    provider.on_stream_reset(recover)
    provider.on_run_update(lambda run: live_update.set() if run.run_id == "run-e" else None)
    try:
        provider.start_stream()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if baselines and provider.stream_state is StreamState.LIVE:
                break
            time.sleep(0.01)
        assert provider.stream_state is StreamState.LIVE
        assert len(baselines) == 1
        assert {
            run.run_id for runs in baselines[0].runs_by_workflow.values() for run in runs
        } == {"run-a", "run-b", "run-c", "run-d"}
        create("run-e")
        assert live_update.wait(5)
    finally:
        provider.close()
