import json
import logging
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest

from runtime.operator import Operator
from runtime.operator.client import GrpcStateProvider, OperatorCallError, StreamState
from runtime.operator.convert_v2 import update_envelope_to_v2
from runtime.operator.models import (
    AgentEvent,
    AgentEventDetailAppended,
    LogDetailAppended,
    LogEntry,
    LogLevel,
    NodeState,
    OperatorUpdateEnvelope,
    RunState,
    RunStatus,
    RunStatusChanged,
    SequencedLogEntry,
)
from runtime.operator.operator import _decode_transport_token, _encode_transport_token
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc
from runtime.operator.scheduler import Scheduler
from runtime.operator.server import TRACE_CHUNK_BYTES, serve


def _event_handle() -> SimpleNamespace:
    return SimpleNamespace(
        cancel_event=threading.Event(),
        result_bundle=None,
        success_quiesced=False,
    )


def _add_run(operator: Operator, run_id: str, *, node_id: str = "agent_1") -> RunState:
    run = RunState(run_id=run_id, flow_name="flow", workflow_id="flow")
    run.nodes[node_id] = NodeState(node_id=node_id, name="Agent", node_type="step")
    with operator._lock:
        operator._runs[run_id] = run
    operator._notify_run(run)
    return run


def _evidence(sequence: int) -> dict:
    return {
        "type": "agent_evidence",
        "node_id": "agent_1",
        "event": {
            "kind": "evidence",
            "invocation_id": "test-invocation",
            "sequence": sequence,
            "event_kind": "code.generated",
            "timestamp_ns": sequence,
            "data": {"iteration": sequence},
        },
    }


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        return sock.getsockname()[1]


class _BarrierOperator(Operator):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.publish_entered = threading.Event()
        self.publish_release = threading.Event()
        self._block_next_publish = False

    def block_next_publication(self) -> None:
        self.publish_entered.clear()
        self.publish_release.clear()
        self._block_next_publish = True

    def _publish_run_locked(self, *args, **kwargs):
        if self._block_next_publish:
            self._block_next_publish = False
            self.publish_entered.set()
            if not self.publish_release.wait(timeout=5):
                raise TimeoutError("Timed out waiting to release publication barrier")
        return super()._publish_run_locked(*args, **kwargs)


class _OrderedDeliveryOperator(Operator):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.observed_publication = threading.Event()
        self._observed_sequence = 0

    def observe_publication(self, sequence: int) -> None:
        self.observed_publication.clear()
        self._observed_sequence = sequence

    def _publish_run_locked(self, *args, **kwargs):
        notifications = super()._publish_run_locked(*args, **kwargs)
        if notifications.sequence == self._observed_sequence:
            self.observed_publication.set()
        return notifications


def test_constructor_failure_stops_started_operator_threads(monkeypatch):
    fixture = Path(__file__).parents[1] / "fixtures" / "sample_workflows.py"
    original_start = Scheduler.start

    def start_then_fail(scheduler: Scheduler) -> None:
        notification_thread = scheduler._operator._notification_thread
        assert notification_thread is not None
        assert notification_thread.is_alive()
        original_start(scheduler)
        raise RuntimeError("scheduler startup failed")

    monkeypatch.setattr(Scheduler, "start", start_then_fail)
    thread_names = {"avalanche-notifications", "avalanche-watcher"}
    baseline = {thread for thread in threading.enumerate() if thread.name in thread_names}

    for _ in range(3):
        with pytest.raises(RuntimeError, match="scheduler startup failed"):
            Operator(
                workflow_paths=[str(fixture)],
                watch=True,
                schedule=True,
            )

        leaked = {
            thread for thread in threading.enumerate() if thread.name in thread_names
        } - baseline
        assert leaked == set()


def test_structural_snapshot_excludes_detail_bodies_while_explicit_read_materializes():
    operator = Operator(watch=False, schedule=False)
    try:
        run = _add_run(operator, "run-1")
        operator._apply_event(
            run.run_id,
            _event_handle(),
            {
                "type": "log",
                "timestamp": 1.0,
                "level": logging.INFO,
                "node_id": "agent_1",
                "message": "hello",
            },
        )
        operator._apply_event(run.run_id, _event_handle(), _evidence(1))
        operator._apply_event(
            run.run_id,
            _event_handle(),
            {
                "type": "agent_evidence",
                "node_id": "agent_1",
                "event": {
                    "kind": "trace_finished",
                    "invocation_id": "test-invocation",
                    "trace": {
                        "status": "completed",
                        "evidence": {"run_id": "predict-run", "complete": True},
                        "steps": [],
                    },
                },
            },
        )

        page = operator.list_run_summaries()
        snapshot = operator.get_run_snapshot(
            run.run_id,
            operator_instance_id=page.operator_instance_id,
            as_of_sequence=page.as_of_sequence,
        )
        assert snapshot is not None
        assert not hasattr(snapshot, "logs")
        assert not hasattr(snapshot.nodes[0], "agent_trace_json")
        assert snapshot.latest_log_sequence == 3
        assert snapshot.nodes[0].trace is not None
        assert snapshot.nodes[0].trace.available is True

        detailed = operator.get_run(run.run_id)
        assert detailed is not None
        assert [entry.message for entry in detailed.logs] == [
            "hello",
            "Agent code.generated iteration=1",
            "Agent trace completed",
        ]
        assert (
            json.loads(detailed.nodes["agent_1"].agent_trace_json)["trace"]["status"]
            == "completed"
        )
    finally:
        operator.close()


def test_latest_run_snapshot_client_is_typed_and_rejects_a_stale_operator_epoch():
    stale_operator = Operator(watch=False, schedule=False)
    stale_operator_instance_id = stale_operator.operator_instance_id
    stale_operator.close()
    operator = Operator(watch=False, schedule=False)
    server = None
    provider = None
    try:
        run = _add_run(operator, "run-latest")
        port = _unused_port()
        server = serve(operator, port=port, block=False)
        provider = GrpcStateProvider(f"localhost:{port}")

        initial = provider.get_latest_run_snapshot(
            run.run_id,
            operator.operator_instance_id,
        )
        operator._apply_event(
            run.run_id,
            _event_handle(),
            {"type": "running", "timestamp": 1.0},
        )
        latest = provider.get_latest_run_snapshot(
            run.run_id,
            operator.operator_instance_id,
        )

        assert initial.summary.run_id == run.run_id
        assert initial.summary.status is RunStatus.PENDING
        assert latest.operator_instance_id == operator.operator_instance_id
        assert latest.as_of_sequence > initial.as_of_sequence
        assert latest.summary.status is RunStatus.RUNNING

        with pytest.raises(OperatorCallError) as error:
            provider.get_latest_run_snapshot(run.run_id, stale_operator_instance_id)
        assert error.value.status is grpc.StatusCode.FAILED_PRECONDITION
    finally:
        if provider is not None:
            provider.close()
        if server is not None:
            server.stop(grace=0).wait()
        operator.close()


def test_log_and_agent_event_pagination_use_exclusive_deduplicating_cursors():
    operator = Operator(watch=False, schedule=False)
    try:
        run = _add_run(operator, "run-1")
        for index in range(3):
            operator._apply_event(
                run.run_id,
                _event_handle(),
                {
                    "type": "log",
                    "timestamp": float(index + 1),
                    "level": logging.INFO,
                    "node_id": "agent_1",
                    "message": f"log-{index + 1}",
                },
            )

        first_logs = operator.list_logs(run.run_id, page_size=2)
        second_logs = operator.list_logs(
            page_token=first_logs.next_page_token,
            page_size=2,
        )
        assert [item.sequence for item in first_logs.logs] == [1, 2]
        assert first_logs.next_page_token
        assert [item.sequence for item in second_logs.logs] == [3]
        assert not second_logs.next_page_token

        operator._apply_event(run.run_id, _event_handle(), _evidence(1))
        operator._apply_event(run.run_id, _event_handle(), _evidence(1))
        operator._apply_event(run.run_id, _event_handle(), _evidence(2))
        first_events = operator.list_agent_events(run.run_id, "agent_1", page_size=1)
        second_events = operator.list_agent_events(
            page_token=first_events.next_page_token,
            page_size=10,
        )
        assert [item.event_sequence for item in first_events.events] == [1]
        assert first_events.next_page_token
        assert [item.event_sequence for item in second_events.events] == [2]
        assert not second_events.next_page_token
    finally:
        operator.close()


def test_exact_node_log_pages_use_the_append_only_node_index():
    class CountingLogs(list):
        def __init__(self, entries):
            super().__init__(entries)
            self.item_reads = 0

        def __getitem__(self, index):
            if isinstance(index, int):
                self.item_reads += 1
            return super().__getitem__(index)

    operator = Operator(watch=False, schedule=False)
    try:
        run = _add_run(operator, "run-sparse-logs")
        timestamp = datetime.now()
        target_entry = LogEntry(
            timestamp=timestamp,
            level=LogLevel.INFO,
            node_id="agent_1",
            message="",
        )
        other_entry = LogEntry(
            timestamp=timestamp,
            level=LogLevel.INFO,
            node_id="other",
            message="",
        )
        with operator._lock:
            for sequence in range(1, 100_001):
                entry = (
                    target_entry if sequence == 50_000 or sequence == 100_000 else other_entry
                )
                operator._append_log_unchecked_locked(run, entry, 0)
            counted_logs = CountingLogs(operator._logs[run.run_id])
            operator._logs[run.run_id] = counted_logs

        missing = operator.list_logs(
            run.run_id,
            page_size=1,
            node_id="missing",
        )
        assert missing.logs == ()
        assert counted_logs.item_reads <= 1

        first = operator.list_logs(
            run.run_id,
            page_size=1,
            node_id="agent_1",
        )
        second = operator.list_logs(
            page_token=first.next_page_token,
            after_sequence=first.logs[-1].sequence,
            page_size=1,
            node_id="agent_1",
        )
        newest = operator.list_logs(
            run.run_id,
            page_size=2,
            node_id="agent_1",
            order=pb.PAGE_ORDER_V2_NEWEST_FIRST,
        )

        assert [item.sequence for item in first.logs] == [50_000]
        assert [item.sequence for item in second.logs] == [100_000]
        assert [item.sequence for item in newest.logs] == [100_000, 50_000]
        assert counted_logs.item_reads <= 8
    finally:
        operator.close()


def test_failed_run_publication_discards_its_log_sequence_index():
    class FailingPublicationOperator(Operator):
        def _publish_run_locked(self, run, *args, **kwargs):
            self._log_sequences_by_node[run.run_id] = {"agent_1": [1]}
            raise RuntimeError("publication failed")

    fixture = Path(__file__).parents[1] / "fixtures" / "sample_workflows.py"
    operator = FailingPublicationOperator(
        workflow_paths=[str(fixture)],
        watch=False,
        schedule=False,
    )
    try:
        with pytest.raises(RuntimeError, match="publication failed"):
            operator.start_run("simple_workflow", run_id="run-failed-publication")

        assert "run-failed-publication" not in operator._log_sequences_by_node
    finally:
        operator.close()


def test_newest_first_pages_reconstruct_filtered_snapshot_without_duplicates():
    operator = Operator(watch=False, schedule=False)
    try:
        run = _add_run(operator, "run-newest")
        with operator._lock:
            run.nodes["agent_2"] = NodeState(
                node_id="agent_2",
                name="Other",
                node_type="step",
            )
        for sequence in range(1, 6):
            operator._apply_event(
                run.run_id,
                _event_handle(),
                {
                    "type": "log",
                    "timestamp": float(sequence),
                    "level": logging.INFO,
                    "node_id": "agent_1" if sequence % 2 else "agent_2",
                    "message": f"log-{sequence}",
                },
            )
            operator._apply_event(
                run.run_id,
                _event_handle(),
                _evidence(sequence),
            )

        snapshot = operator.get_latest_run_snapshot(
            run.run_id,
            operator_instance_id=operator.operator_instance_id,
        )
        assert snapshot is not None
        operator._apply_event(
            run.run_id,
            _event_handle(),
            {
                "type": "log",
                "timestamp": 100.0,
                "level": logging.INFO,
                "node_id": "agent_1",
                "message": "after-snapshot",
            },
        )
        operator._apply_event(run.run_id, _event_handle(), _evidence(6))

        forward_logs = operator.list_logs(
            page_token=snapshot.log_page_token,
            page_size=100,
            node_id="agent_1",
        )
        newest_log_sequences = []
        newest_log_descriptors = []
        token = snapshot.log_page_token
        before_sequence = 0
        while token:
            page = operator.list_logs(
                page_token=token,
                page_size=2,
                before_sequence=before_sequence,
                node_id="agent_1",
                order=pb.PAGE_ORDER_V2_NEWEST_FIRST,
            )
            newest_log_sequences.extend(item.sequence for item in page.logs)
            newest_log_descriptors.extend(page.logs)
            before_sequence = page.logs[-1].sequence if page.logs else 0
            token = page.next_page_token

        forward_events = operator.list_agent_events(
            page_token=snapshot.nodes[0].event_page_token,
            page_size=100,
        )
        newest_event_sequences = []
        token = snapshot.nodes[0].event_page_token
        before_event_sequence = 0
        while token:
            page = operator.list_agent_events(
                page_token=token,
                page_size=2,
                before_event_sequence=before_event_sequence,
                order=pb.PAGE_ORDER_V2_NEWEST_FIRST,
            )
            newest_event_sequences.extend(item.event_sequence for item in page.events)
            before_event_sequence = page.events[-1].event_sequence if page.events else 0
            token = page.next_page_token

        forward_log_sequences = [item.sequence for item in forward_logs.logs]
        forward_event_sequences = [item.event_sequence for item in forward_events.events]
        assert newest_log_sequences[::-1] == forward_log_sequences
        assert newest_event_sequences[::-1] == forward_event_sequences
        assert len(newest_log_sequences) == len(set(newest_log_sequences))
        assert len(newest_event_sequences) == len(set(newest_event_sequences))
        assert {item.node_id for item in newest_log_descriptors} == {"agent_1"}
        assert all(
            sequence <= snapshot.latest_log_sequence for sequence in newest_log_sequences
        )
        bodies = []
        for descriptor in newest_log_descriptors:
            body_token = _decode_transport_token(
                descriptor.body_token,
                "log-body",
            )
            assert body_token["as_of_sequence"] == snapshot.as_of_sequence
            bodies.append(operator.read_detail(descriptor.body_token))
        assert b"after-snapshot" not in bodies
    finally:
        operator.close()


def test_detail_continuations_reject_cursor_filter_and_direction_changes():
    operator = Operator(watch=False, schedule=False)
    try:
        run = _add_run(operator, "run-cursors")
        for sequence in range(1, 4):
            operator._apply_event(
                run.run_id,
                _event_handle(),
                {
                    "type": "log",
                    "timestamp": float(sequence),
                    "level": logging.INFO,
                    "node_id": "agent_1",
                    "message": f"log-{sequence}",
                },
            )
            operator._apply_event(run.run_id, _event_handle(), _evidence(sequence))
        snapshot = operator.get_latest_run_snapshot(
            run.run_id,
            operator_instance_id=operator.operator_instance_id,
        )
        assert snapshot is not None

        first_log_page = operator.list_logs(
            page_token=snapshot.log_page_token,
            page_size=1,
            node_id="agent_1",
        )
        assert first_log_page.next_page_token
        with pytest.raises(ValueError, match="cursor"):
            operator.list_logs(
                page_token=first_log_page.next_page_token,
                after_sequence=first_log_page.logs[-1].sequence + 1,
                page_size=1,
                node_id="agent_1",
            )
        with pytest.raises(ValueError, match="filter"):
            operator.list_logs(
                page_token=first_log_page.next_page_token,
                after_sequence=first_log_page.logs[-1].sequence,
                page_size=1,
                node_id="agent_2",
            )

        newest_log_page = operator.list_logs(
            page_token=snapshot.log_page_token,
            page_size=1,
            node_id="agent_1",
            order=pb.PAGE_ORDER_V2_NEWEST_FIRST,
        )
        assert newest_log_page.next_page_token
        with pytest.raises(ValueError, match="order"):
            operator.list_logs(
                page_token=newest_log_page.next_page_token,
                page_size=1,
                node_id="agent_1",
            )

        first_event_page = operator.list_agent_events(
            page_token=snapshot.nodes[0].event_page_token,
            page_size=1,
        )
        assert first_event_page.next_page_token
        with pytest.raises(ValueError, match="cursor"):
            operator.list_agent_events(
                page_token=first_event_page.next_page_token,
                after_event_sequence=first_event_page.events[-1].event_sequence + 1,
                page_size=1,
            )

        decoded = _decode_transport_token(
            first_event_page.next_page_token,
            "events",
        )
        decoded["future_token_field"] = {"version": 2}
        compatible_token = _encode_transport_token(**decoded)
        compatible_page = operator.list_agent_events(
            page_token=compatible_token,
            after_event_sequence=first_event_page.events[-1].event_sequence,
            page_size=1,
        )
        assert compatible_page.events
    finally:
        operator.close()


def test_detail_pagination_requires_snapshot_issued_page_tokens():
    operator = Operator(watch=False, schedule=False)
    server = None
    channel = None
    try:
        port = _unused_port()
        server = serve(operator, port=port, block=False)
        channel = grpc.insecure_channel(f"localhost:{port}")
        stub = pb_grpc.OperatorServiceV2Stub(channel)

        bogus_continuation = pb.ContinuationRefV2(
            scope_ref=pb.ScopeReferenceV2(reference=operator.operator_instance_id),
            continuation_id="not-a-snapshot-issued-token",
        )
        for invoke in (
            lambda: stub.ListRunActivity(pb.ListRunActivityRequestV2()),
            lambda: stub.ListRunActivity(
                pb.ListRunActivityRequestV2(run_id="run-1", node_id="agent_1")
            ),
        ):
            with pytest.raises(grpc.RpcError) as error:
                invoke()
            assert error.value.code() is grpc.StatusCode.NOT_FOUND

        for invoke in (
            lambda: stub.ListRunActivity(
                pb.ListRunActivityRequestV2(continuation=bogus_continuation)
            ),
            lambda: stub.ListRunActivity(
                pb.ListRunActivityRequestV2(
                    run_id="run-1",
                    node_id="agent_1",
                    continuation=bogus_continuation,
                )
            ),
        ):
            with pytest.raises(grpc.RpcError) as error:
                invoke()
            assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT
            assert "Invalid" in (error.value.details() or "")
    finally:
        if channel is not None:
            channel.close()
        if server is not None:
            server.stop(grace=0).wait()
        operator.close()


def test_run_summary_page_token_is_stable_when_new_runs_arrive():
    operator = Operator(watch=False, schedule=False)
    try:
        for index in range(1, 5):
            _add_run(operator, f"run-{index}")

        first = operator.list_run_summaries(page_size=2)
        assert [item.run_id for item in first.runs] == ["run-4", "run-3"]
        assert first.next_page_token

        _add_run(operator, "run-5")
        second = operator.list_run_summaries(
            page_size=2,
            page_token=first.next_page_token,
        )
        assert second.as_of_sequence == first.as_of_sequence
        assert [item.run_id for item in second.runs] == ["run-2", "run-1"]
        assert {item.run_id for item in first.runs}.isdisjoint(
            item.run_id for item in second.runs
        )
    finally:
        operator.close()


def test_summary_pages_and_snapshots_remain_exact_during_continuous_updates():
    operator = Operator(
        watch=False,
        schedule=False,
        structural_baseline_capacity=2,
    )
    try:
        runs = {
            run_id: _add_run(operator, run_id)
            for run_id in ("run-1", "run-2", "run-3", "run-4")
        }
        first = operator.list_run_summaries(page_size=2)
        initial_snapshot = operator.get_run_snapshot(
            "run-1",
            operator_instance_id=first.operator_instance_id,
            as_of_sequence=first.as_of_sequence,
        )
        assert initial_snapshot is not None
        assert initial_snapshot.summary.status == RunStatus.PENDING
        assert initial_snapshot.nodes[0].trace is None

        operator._apply_event(
            runs["run-1"].run_id, _event_handle(), {"type": "running", "timestamp": 1.0}
        )
        for event_sequence in range(1, 6):
            operator._apply_event(
                runs["run-1"].run_id, _event_handle(), _evidence(event_sequence)
            )
            continuation = operator.list_run_summaries(
                page_size=2,
                page_token=first.next_page_token,
            )
            retained_snapshot = operator.get_run_snapshot(
                "run-1",
                operator_instance_id=first.operator_instance_id,
                as_of_sequence=first.as_of_sequence,
            )

            assert continuation.as_of_sequence == first.as_of_sequence
            assert [item.run_id for item in continuation.runs] == ["run-2", "run-1"]
            assert continuation.runs[-1].status == RunStatus.PENDING
            assert retained_snapshot == initial_snapshot

        current = operator.list_run_summaries(page_size=10)
        current_snapshot = operator.get_run_snapshot(
            "run-1",
            operator_instance_id=current.operator_instance_id,
            as_of_sequence=current.as_of_sequence,
        )
        assert current.as_of_sequence > first.as_of_sequence
        assert current_snapshot is not None
        assert current_snapshot.summary.status == RunStatus.RUNNING
        assert current_snapshot.nodes[0].trace is not None
        assert current_snapshot.nodes[0].trace.event_count == 5
    finally:
        operator.close()


def test_evicted_structural_baseline_requires_grpc_restart():
    operator = Operator(
        watch=False,
        schedule=False,
        structural_baseline_capacity=1,
    )
    server = None
    channel = None
    try:
        first_run = _add_run(operator, "run-1")
        _add_run(operator, "run-2")
        evicted = operator.list_run_summaries(page_size=1)
        assert evicted.next_page_token

        operator._apply_event(
            first_run.run_id, _event_handle(), {"type": "running", "timestamp": 1.0}
        )
        current = operator.list_run_summaries(page_size=10)
        assert current.as_of_sequence > evicted.as_of_sequence

        port = _unused_port()
        server = serve(operator, port=port, block=False)
        channel = grpc.insecure_channel(f"localhost:{port}")
        grpc.channel_ready_future(channel).result(timeout=5)
        stub = pb_grpc.OperatorServiceV2Stub(channel)

        scope = pb.ScopeReferenceV2(reference=operator.operator_instance_id)
        baseline = stub.ListRunSummaries(pb.ListRunSummariesRequestV2(page_size=1))
        wrong_generation = baseline.cursor.stream_generation ^ 1

        with pytest.raises(grpc.RpcError) as snapshot_error:
            stub.ListRunSummaries(
                pb.ListRunSummariesRequestV2(
                    page_size=1,
                    continuation=pb.ContinuationRefV2(
                        scope_ref=scope,
                        continuation_id=baseline.next_page.continuation_id,
                        cursor=pb.LifecycleCursorV2(
                            stream=baseline.cursor.stream,
                            stream_generation=wrong_generation,
                            source_sequence=baseline.cursor.source_sequence,
                        ),
                    ),
                )
            )
        assert snapshot_error.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        with pytest.raises(grpc.RpcError) as page_error:
            stub.ListRunSummaries(
                pb.ListRunSummariesRequestV2(
                    page_size=1,
                    continuation=pb.ContinuationRefV2(
                        scope_ref=scope,
                        continuation_id=evicted.next_page_token,
                        cursor=pb.LifecycleCursorV2(
                            stream="run-summaries",
                            stream_generation=baseline.cursor.stream_generation,
                            source_sequence=evicted.as_of_sequence,
                        ),
                    ),
                )
            )
        assert page_error.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        snapshot = stub.GetRunSnapshot(
            pb.GetRunSnapshotRequestV2(run_id=first_run.run_id)
        )
        assert snapshot.cursor.source_sequence == current.as_of_sequence
        assert snapshot.summary.status == RunStatus.RUNNING.value
        assert snapshot.scope_ref.reference == operator.operator_instance_id
    finally:
        if channel is not None:
            channel.close()
        if server is not None:
            server.stop(grace=0).wait()
        operator.close()


def test_start_run_publication_blocks_pagination_until_creation_is_revisioned():
    fixture = Path(__file__).parents[1] / "fixtures" / "sample_workflows.py"
    operator = _BarrierOperator(
        workflow_paths=[str(fixture)],
        watch=False,
        schedule=False,
    )
    start_errors = []
    reader_done = threading.Event()
    page_holder = []
    subscription = operator.subscribe_operator_updates()
    operator.block_next_publication()

    def start_run() -> None:
        try:
            operator.start_run("simple_workflow", run_id="run-publishing")
        except BaseException as exc:
            start_errors.append(exc)

    def read_page() -> None:
        page_holder.append(operator.list_run_summaries(page_size=10))
        reader_done.set()

    starter = threading.Thread(target=start_run)
    reader = threading.Thread(target=read_page)
    try:
        starter.start()
        assert operator.publish_entered.wait(timeout=5)
        reader.start()
        assert not reader_done.wait(timeout=0.1)

        operator.publish_release.set()
        starter.join(timeout=10)
        reader.join(timeout=5)
        assert not starter.is_alive()
        assert not reader.is_alive()
        assert start_errors == []
        first = subscription.get(timeout=5)
        assert first.update.change.summary.status == RunStatus.REQUESTING

        page = page_holder[0]
        summary = next(item for item in page.runs if item.run_id == "run-publishing")
        assert summary.created_sequence > 0
        assert first.update.sequence == summary.created_sequence
        assert summary.revision >= summary.created_sequence
        assert page.as_of_sequence >= summary.revision
    finally:
        operator.publish_release.set()
        starter.join(timeout=1)
        if reader.ident is not None:
            reader.join(timeout=1)
        operator.unsubscribe_operator_updates(subscription)
        operator.close()


def test_agent_detail_and_watermarks_become_visible_in_one_transaction():
    operator = _BarrierOperator(watch=False, schedule=False)
    run = _add_run(operator, "run-atomic")
    apply_errors = []
    reader_done = threading.Event()
    observed = {}
    operator.block_next_publication()

    def apply_event() -> None:
        try:
            operator._apply_event(run.run_id, _event_handle(), _evidence(1))
        except BaseException as exc:
            apply_errors.append(exc)

    def read_detail() -> None:
        snapshot = operator.get_latest_run_snapshot(
            run.run_id,
            operator_instance_id=operator.operator_instance_id,
        )
        observed["snapshot"] = snapshot
        assert snapshot is not None
        observed["logs"] = operator.list_logs(
            page_token=snapshot.log_page_token,
        )
        observed["events"] = operator.list_agent_events(
            page_token=snapshot.nodes[0].event_page_token,
        )
        reader_done.set()

    publisher = threading.Thread(target=apply_event)
    reader = threading.Thread(target=read_detail)
    try:
        publisher.start()
        assert operator.publish_entered.wait(timeout=5)
        reader.start()
        assert not reader_done.wait(timeout=0.1)

        operator.publish_release.set()
        publisher.join(timeout=5)
        reader.join(timeout=5)
        assert not publisher.is_alive()
        assert not reader.is_alive()
        assert apply_errors == []

        snapshot = observed["snapshot"]
        logs = observed["logs"]
        events = observed["events"]
        assert snapshot is not None
        assert snapshot.as_of_sequence == operator.current_sequence
        assert logs.as_of_sequence == snapshot.as_of_sequence
        assert events.as_of_sequence == snapshot.as_of_sequence
        assert snapshot.nodes[0].revision == snapshot.as_of_sequence
        assert snapshot.nodes[0].trace.revision == snapshot.as_of_sequence
        assert snapshot.latest_log_sequence == logs.logs[-1].sequence == 1
        assert snapshot.nodes[0].trace.event_count == len(events.events) == 1
    finally:
        operator.publish_release.set()
        publisher.join(timeout=1)
        if reader.ident is not None:
            reader.join(timeout=1)
        operator.close()


def test_concurrent_publishers_dispatch_detail_callbacks_and_updates_in_order():
    operator = _OrderedDeliveryOperator(watch=False, schedule=False)
    run = _add_run(operator, "run-ordered")
    subscription = operator.subscribe_operator_updates(
        operator.operator_instance_id, operator.current_sequence
    )
    first_callback_entered = threading.Event()
    release_first_callback = threading.Event()
    detail_messages = []
    log_callback_messages = []
    publisher_errors = []

    def on_detail(detail) -> None:
        if not isinstance(detail, LogDetailAppended):
            return
        message = detail.log.message
        if message == "N":
            first_callback_entered.set()
            if not release_first_callback.wait(timeout=5):
                raise TimeoutError("Timed out waiting to release first callback")
        detail_messages.append(message)

    def on_log(entry) -> None:
        log_callback_messages.append(entry.message)

    def publish(message: str, timestamp: float) -> None:
        try:
            operator._apply_event(
                run.run_id,
                _event_handle(),
                {
                    "type": "log",
                    "timestamp": timestamp,
                    "level": logging.INFO,
                    "node_id": "agent_1",
                    "message": message,
                },
            )
        except BaseException as exc:
            publisher_errors.append(exc)

    operator.on_detail_update(on_detail)
    operator.on_log(on_log)
    first_sequence = operator.current_sequence + 1
    operator.observe_publication(first_sequence + 1)
    publisher_n = threading.Thread(target=publish, args=("N", 1.0))
    publisher_n1 = threading.Thread(target=publish, args=("N+1", 2.0))
    try:
        publisher_n.start()
        assert first_callback_entered.wait(timeout=5)

        publisher_n1.start()
        assert operator.observed_publication.wait(timeout=5)
        assert subscription.empty()
        assert publisher_n.is_alive()
        assert publisher_n1.is_alive()

        release_first_callback.set()
        publisher_n.join(timeout=5)
        publisher_n1.join(timeout=5)
        assert not publisher_n.is_alive()
        assert not publisher_n1.is_alive()
        assert publisher_errors == []

        subscriber_updates = [subscription.get(timeout=5), subscription.get(timeout=5)]
        assert [item.update.sequence for item in subscriber_updates] == [
            first_sequence,
            first_sequence + 1,
        ]
        assert [item.update.change.log.sequence for item in subscriber_updates] == [1, 2]
        assert detail_messages == ["N", "N+1"]
        assert log_callback_messages == ["N", "N+1"]
    finally:
        release_first_callback.set()
        if publisher_n.ident is not None:
            publisher_n.join(timeout=1)
        if publisher_n1.ident is not None:
            publisher_n1.join(timeout=1)
        operator.unsubscribe_operator_updates(subscription)
        operator.close()
    assert not operator._notification_thread.is_alive()


def test_close_keeps_dispatcher_alive_for_notification_from_delayed_drain():
    operator = Operator(watch=False, schedule=False, cancel_grace=0)
    run = _add_run(operator, "run-delayed-close")
    subscription = operator.subscribe_operator_updates(
        operator.operator_instance_id, operator.current_sequence
    )
    callback_statuses = []
    drain_entered = threading.Event()
    release_drain = threading.Event()

    def delayed_drain() -> None:
        drain_entered.set()
        if not release_drain.wait(timeout=10):
            raise TimeoutError("Timed out waiting to release delayed drain")
        operator._apply_event(
            run.run_id,
            handle,
            {"type": "terminal", "status": "cancelled"},
        )

    operator.on_run_update(lambda snapshot: callback_statuses.append(snapshot.status))
    drain = threading.Thread(target=delayed_drain)
    handle = SimpleNamespace(
        process=SimpleNamespace(pid=None, exitcode=None),
        cancel_event=threading.Event(),
        start_event=threading.Event(),
        windows_job=None,
        drain_thread=drain,
        preparation_thread=None,
        result_bundle=operator._result_store.prepare(),
        success_quiesced=False,
    )
    with operator._lock:
        operator._active_runs[run.run_id] = handle

    try:
        drain.start()
        assert drain_entered.wait(timeout=5)
        operator.close()

        assert drain.is_alive()
        assert operator._notification_thread.is_alive()
        assert operator._notification_shutdown_thread is not None
        assert operator._notification_shutdown_thread.is_alive()
        assert subscription.empty()

        release_drain.set()
        drain.join(timeout=5)
        operator._notification_shutdown_thread.join(timeout=5)
        operator._notification_thread.join(timeout=5)

        assert not drain.is_alive()
        assert not operator._notification_shutdown_thread.is_alive()
        assert not operator._notification_thread.is_alive()
        final = subscription.get(timeout=5)
        assert isinstance(final.update.change, RunStatusChanged)
        assert final.update.change.status == RunStatus.CANCELLED
        assert callback_statuses == [RunStatus.CANCELLED]
    finally:
        release_drain.set()
        if drain.ident is not None:
            drain.join(timeout=1)
        operator.unsubscribe_operator_updates(subscription)
        operator.close()


def test_read_trace_streams_more_than_four_mib_in_bounded_revisioned_chunks():
    operator = Operator(watch=False, schedule=False)
    server = None
    channel = None
    try:
        run = _add_run(operator, "run-large")
        operator._apply_event(run.run_id, _event_handle(), _evidence(1))
        operator._apply_event(
            run.run_id,
            _event_handle(),
            {
                "type": "agent_evidence",
                "node_id": "agent_1",
                "event": {
                    "kind": "trace_finished",
                    "invocation_id": "test-invocation",
                    "trace": {
                        "status": "completed",
                        "evidence": {"complete": True},
                        "payload": "x" * (4 * 1024 * 1024 + 17),
                    },
                },
            },
        )
        expected = operator.read_trace(
            run.run_id,
            "agent_1",
            operator_instance_id=operator.operator_instance_id,
        )

        port = _unused_port()
        server = serve(operator, port=port, block=False)
        channel = grpc.insecure_channel(f"localhost:{port}")
        grpc.channel_ready_future(channel).result(timeout=5)
        stub = pb_grpc.OperatorServiceV2Stub(channel)
        summaries = stub.ListRunSummaries(pb.ListRunSummariesRequestV2(page_size=10))
        snapshot = stub.GetRunSnapshot(pb.GetRunSnapshotRequestV2(run_id=run.run_id))
        logs = stub.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id=run.run_id,
                continuation=snapshot.log_continuation,
                page_size=1,
            )
        )
        events = stub.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id=run.run_id,
                node_id="agent_1",
                continuation=snapshot.nodes[0].activity_continuation,
                page_size=10,
            )
        )
        trace_ref = snapshot.nodes[0].trace.detail_ref
        chunks = list(
            stub.ReadActivityDetail(
                pb.ReadActivityDetailRequestV2(detail_ref=trace_ref)
            )
        )

        assert summaries.scope_ref.reference == operator.operator_instance_id
        assert [item.run_id for item in summaries.runs] == [run.run_id]
        assert snapshot.scope_ref.reference == operator.operator_instance_id
        assert snapshot.nodes[0].trace.revision == expected.revision
        assert [item.run_sequence for item in logs.activities] == [1]
        assert logs.HasField("next_page")
        assert [item.run_sequence for item in events.activities] == [1]
        assert len(expected.data) > 4 * 1024 * 1024
        assert len(chunks) > 4
        assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
        assert all(0 < len(chunk.data) <= TRACE_CHUNK_BYTES for chunk in chunks)
        assert trace_ref.object_uri == (
            f"local://trace/{run.run_id}/agent_1/{expected.revision}"
        )
        assert [chunk.eof for chunk in chunks] == [False] * (len(chunks) - 1) + [True]
        assert b"".join(chunk.data for chunk in chunks) == expected.data
    finally:
        if channel is not None:
            channel.close()
        if server is not None:
            server.stop(grace=0).wait()
        operator.close()


def test_max_log_and_large_agent_event_use_bounded_live_and_hydration_transport():
    operator = Operator(watch=False, schedule=False)
    server = None
    live = None
    hydrated = None
    try:
        run = _add_run(operator, "run-large-detail")
        port = _unused_port()
        server = serve(operator, port=port, block=False)
        live = GrpcStateProvider(f"localhost:{port}")
        observed = []
        live.on_run_update(observed.append)
        details = []
        live.on_detail_update(details.append)
        live.start_stream()
        deadline = time.monotonic() + 5
        while live.stream_state is not StreamState.LIVE:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        large_log = "L" * 65_536
        large_event_payload = "E" * (5 * 1024 * 1024 + 29)
        operator._apply_event(
            run.run_id,
            _event_handle(),
            {
                "type": "log",
                "timestamp": 1.0,
                "level": logging.INFO,
                "node_id": "agent_1",
                "message": large_log,
            },
        )
        operator._apply_event(
            run.run_id,
            _event_handle(),
            {
                "type": "agent_evidence",
                "node_id": "agent_1",
                "event": {
                    "kind": "evidence",
                    "invocation_id": "test-invocation",
                    "sequence": 1,
                    "event_kind": "large.event",
                    "timestamp_ns": 1,
                    "data": {"payload": large_event_payload},
                },
            },
        )

        key = (run.run_id, "agent_1")
        deadline = time.monotonic() + 10
        while True:
            matching = [item for item in observed if item.run_id == run.run_id]
            with live._state_lock:
                live_logs = live._log_entries.get(run.run_id)
                live_events = live._agent_events.get(key)
            has_log_detail = any(
                isinstance(detail, LogDetailAppended) and detail.log.message == large_log
                for detail in details
            )
            has_agent_detail = any(
                isinstance(detail, AgentEventDetailAppended)
                and json.loads(detail.event.event_json)["data"]["payload"]
                == large_event_payload
                for detail in details
            )
            if matching and live_logs and live_events and has_log_detail and has_agent_detail:
                latest = matching[-1]
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert latest.logs == []
        assert latest.nodes["agent_1"].agent_trace_json is None
        assert large_log in [entry.message for entry in live_logs]
        assert live_events[0]["data"]["payload"] == large_event_payload
        assert any(
            isinstance(detail, LogDetailAppended) and detail.log.message == large_log
            for detail in details
        )
        assert any(
            isinstance(detail, AgentEventDetailAppended)
            and json.loads(detail.event.event_json)["data"]["payload"] == large_event_payload
            for detail in details
        )
        snapshot = live._stub.GetRunSnapshot(
            pb.GetRunSnapshotRequestV2(run_id=run.run_id)
        )
        log_page = live._stub.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id=run.run_id,
                continuation=snapshot.log_continuation,
                page_size=10,
            )
        )
        event_page = live._stub.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id=run.run_id,
                node_id="agent_1",
                continuation=snapshot.nodes[0].activity_continuation,
                page_size=10,
            )
        )
        assert log_page.ByteSize() < 4 * 1024 * 1024
        assert event_page.ByteSize() < 4 * 1024 * 1024
        large_log_descriptor = next(
            item for item in log_page.activities if item.size_bytes == len(large_log)
        )
        large_event_descriptor = event_page.activities[0]
        log_chunks = list(
            live._stub.ReadActivityDetail(
                pb.ReadActivityDetailRequestV2(
                    detail_ref=large_log_descriptor.detail_ref
                )
            )
        )
        assert len(log_chunks) == 1
        assert log_chunks[0].eof is True
        event_chunks = list(
            live._stub.ReadActivityDetail(
                pb.ReadActivityDetailRequestV2(
                    detail_ref=large_event_descriptor.detail_ref
                )
            )
        )
        assert len(event_chunks) > 4
        assert all(len(chunk.data) <= TRACE_CHUNK_BYTES for chunk in event_chunks)
        assert [chunk.eof for chunk in event_chunks] == [False] * (len(event_chunks) - 1) + [
            True
        ]

        scope_ref = pb.ScopeReferenceV2(reference=operator.operator_instance_id)

        def _cursor_for(sequence: int) -> pb.LifecycleCursorV2:
            return pb.LifecycleCursorV2(
                stream="operator-events",
                stream_generation=1,
                source_sequence=sequence,
            )

        for update in operator._stream_history:
            envelope_message = update_envelope_to_v2(
                OperatorUpdateEnvelope(
                    operator_instance_id=operator.operator_instance_id,
                    update=update,
                ),
                scope_ref=scope_ref,
                cursor_for=_cursor_for,
            )
            assert envelope_message.ByteSize() < 4 * 1024 * 1024

        hydrated = GrpcStateProvider(f"localhost:{port}")
        detail = hydrated.get_run(run.run_id)
        assert detail is not None
        assert large_log in [entry.message for entry in detail.logs]
        detail_envelope = json.loads(detail.nodes["agent_1"].agent_trace_json)
        assert detail_envelope["events"][0]["data"]["payload"] == large_event_payload
    finally:
        if hydrated is not None:
            hydrated.close()
        if live is not None:
            live.close()
        if server is not None:
            server.stop(grace=0).wait()
        operator.close()


def test_agent_detail_ingestion_rejects_oversize_and_excess_depth_before_retention():
    operator = Operator(
        watch=False,
        schedule=False,
        max_agent_event_bytes=256,
        max_trace_body_bytes=256,
        max_node_detail_bytes=512,
        max_run_log_bytes=512,
        max_run_detail_bytes=768,
    )
    try:
        run = _add_run(operator, "run-detail-body-limits")
        oversized = _evidence(1)
        oversized["event"]["data"] = {"payload": "x" * 512}
        operator._apply_event(run.run_id, _event_handle(), oversized)

        nested = {}
        cursor = nested
        for _ in range(70):
            child = {}
            cursor["child"] = child
            cursor = child
        too_deep = _evidence(2)
        too_deep["event"]["data"] = nested
        operator._apply_event(run.run_id, _event_handle(), too_deep)

        operator._apply_event(
            run.run_id,
            _event_handle(),
            {
                "type": "agent_evidence",
                "node_id": "agent_1",
                "event": {
                    "kind": "trace_finished",
                    "invocation_id": "test-invocation",
                    "trace": {"payload": "t" * 512},
                },
            },
        )

        assert operator._agent_events.get((run.run_id, "agent_1"), []) == []
        assert operator._logs.get(run.run_id, []) == []
        assert operator._trace_bodies.get((run.run_id, "agent_1"), {}) == {}
        assert operator._run_detail_bytes.get(run.run_id, 0) == 0
    finally:
        operator.close()


def test_detail_ingestion_enforces_cumulative_node_run_and_log_quotas():
    operator = Operator(
        watch=False,
        schedule=False,
        max_agent_event_bytes=256,
        max_trace_body_bytes=256,
        max_node_detail_bytes=512,
        max_run_log_bytes=512,
        max_run_detail_bytes=900,
    )
    try:
        run = _add_run(operator, "run-detail-quotas")
        run.nodes["agent_2"] = NodeState(
            node_id="agent_2",
            name="Agent 2",
            node_type="step",
        )
        for sequence in range(1, 21):
            node_id = "agent_1" if sequence % 2 else "agent_2"
            operator._apply_event(
                run.run_id,
                _event_handle(),
                {
                    "type": "agent_evidence",
                    "node_id": node_id,
                    "event": {
                        "kind": "evidence",
                        "invocation_id": "test-invocation",
                        "sequence": sequence,
                        "event_kind": "code.generated",
                        "timestamp_ns": sequence,
                        "data": {"payload": "x" * 64},
                    },
                },
            )

        retained = sum(
            len(events)
            for (run_id, _), events in operator._agent_events.items()
            if run_id == run.run_id
        )
        assert 0 < retained < 20
        assert operator._run_detail_bytes[run.run_id] <= 900
        assert all(
            size <= 512
            for (run_id, _), size in operator._node_detail_bytes.items()
            if run_id == run.run_id
        )
    finally:
        operator.close()

    log_operator = Operator(
        watch=False,
        schedule=False,
        max_agent_event_bytes=256,
        max_trace_body_bytes=256,
        max_node_detail_bytes=512,
        max_run_log_bytes=128,
        max_run_detail_bytes=512,
    )
    try:
        run = _add_run(log_operator, "run-log-quota")
        for sequence in range(1, 10):
            event = {
                "type": "log",
                "timestamp": float(sequence),
                "level": logging.INFO,
                "node_id": "agent_1",
                "message": "l" * 48,
            }
            if sequence <= 2:
                log_operator._apply_event(run.run_id, _event_handle(), event)
            else:
                with pytest.raises(RuntimeError, match="run logs exceed"):
                    log_operator._apply_event(run.run_id, _event_handle(), event)
                break
        assert len(log_operator._logs[run.run_id]) == 2
        assert log_operator._run_log_bytes[run.run_id] == 96
    finally:
        log_operator.close()

    count_operator = Operator(
        watch=False,
        schedule=False,
        max_run_log_entries=2,
    )
    try:
        run = _add_run(count_operator, "run-log-count-quota")
        event = {
            "type": "log",
            "timestamp": 1.0,
            "level": logging.INFO,
            "node_id": "agent_1",
            "message": "",
        }
        count_operator._apply_event(run.run_id, _event_handle(), event)
        count_operator._apply_event(run.run_id, _event_handle(), event)
        with pytest.raises(RuntimeError, match="run logs exceed 2 entry limit"):
            count_operator._apply_event(run.run_id, _event_handle(), event)
        assert len(count_operator._logs[run.run_id]) == 2
        assert count_operator._run_log_bytes[run.run_id] == 0
    finally:
        count_operator.close()

    trace_operator = Operator(
        watch=False,
        schedule=False,
        max_agent_event_bytes=256,
        max_trace_body_bytes=256,
        max_node_detail_bytes=512,
        max_run_log_bytes=512,
        max_run_detail_bytes=768,
    )
    try:
        run = _add_run(trace_operator, "run-trace-quota")
        for revision in range(3):
            trace_operator._apply_event(
                run.run_id,
                _event_handle(),
                {
                    "type": "agent_evidence",
                    "node_id": "agent_1",
                    "event": {
                        "kind": "trace_finished",
                        "invocation_id": "test-invocation",
                        "trace": {
                            "status": "completed",
                            "payload": str(revision) * 180,
                        },
                    },
                },
            )
        versions = trace_operator._trace_bodies[(run.run_id, "agent_1")]
        assert len(versions) == 2
        assert trace_operator._node_detail_bytes[(run.run_id, "agent_1")] <= 512
        assert trace_operator._run_detail_bytes[run.run_id] <= 768
    finally:
        trace_operator.close()


def test_read_trace_rejects_reused_identity_from_previous_operator_epoch():
    first = Operator(watch=False, schedule=False)
    second = Operator(watch=False, schedule=False)
    server = None
    channel = None

    def seed_trace(operator: Operator, marker: str):
        run = _add_run(operator, "run-reused")
        operator._apply_event(run.run_id, _event_handle(), _evidence(1))
        operator._apply_event(
            run.run_id,
            _event_handle(),
            {
                "type": "agent_evidence",
                "node_id": "agent_1",
                "event": {
                    "kind": "trace_finished",
                    "invocation_id": "test-invocation",
                    "trace": {
                        "status": "completed",
                        "evidence": {"complete": True},
                        "marker": marker,
                    },
                },
            },
        )
        return operator.read_trace(
            run.run_id,
            "agent_1",
            operator_instance_id=operator.operator_instance_id,
        ).revision

    try:
        revision = seed_trace(first, "first")
        stale_epoch = first.operator_instance_id
        first.close()

        assert seed_trace(second, "second") == revision
        port = _unused_port()
        server = serve(second, port=port, block=False)
        channel = grpc.insecure_channel(f"localhost:{port}")
        grpc.channel_ready_future(channel).result(timeout=5)
        stub = pb_grpc.OperatorServiceV2Stub(channel)

        stale_ref = pb.ActivityDetailRefV2(
            run_id="run-reused",
            scope_ref=pb.ScopeReferenceV2(reference=stale_epoch),
            activity_id=f"trace:agent_1:{revision}",
            object_uri=f"local://trace/run-reused/agent_1/{revision}",
            object_key=f"run-reused/agent_1/{revision}",
        )
        with pytest.raises(grpc.RpcError) as error:
            list(
                stub.ReadActivityDetail(
                    pb.ReadActivityDetailRequestV2(detail_ref=stale_ref)
                )
            )
        assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        detail_ref = pb.ActivityDetailRefV2(
            run_id="run-reused",
            scope_ref=pb.ScopeReferenceV2(reference=second.operator_instance_id),
            activity_id=f"trace:agent_1:{revision}",
            object_uri=f"local://trace/run-reused/agent_1/{revision}",
            object_key=f"run-reused/agent_1/{revision}",
        )
        chunks = list(
            stub.ReadActivityDetail(
                pb.ReadActivityDetailRequestV2(detail_ref=detail_ref)
            )
        )
        trace = json.loads(b"".join(chunk.data for chunk in chunks))
        assert trace["marker"] == "second"
    finally:
        if channel is not None:
            channel.close()
        if server is not None:
            server.stop(grace=0).wait()
        first.close()
        second.close()


def test_detail_page_lookup_only_touches_page_plus_one_items():
    class GuardedList(list):
        def __init__(self, values):
            super().__init__(values)
            self.slices = []
            self.index_reads = 0

        def __iter__(self):
            raise AssertionError("page lookup scanned the full detail list")

        def __getitem__(self, index):
            if isinstance(index, slice):
                self.slices.append(index)
            else:
                self.index_reads += 1
            return super().__getitem__(index)

    operator = Operator(watch=False, schedule=False)
    try:
        run = _add_run(operator, "run-page-bound")
        logs = GuardedList(
            [
                SequencedLogEntry(
                    sequence=index + 1,
                    entry=LogEntry(
                        timestamp=datetime.fromtimestamp(1),
                        level=LogLevel.INFO,
                        node_id="agent_1",
                        message="x",
                    ),
                    size_bytes=1,
                )
                for index in range(10_000)
            ]
        )
        events = GuardedList(
            [
                AgentEvent(
                    invocation_id="test-invocation",
                    event_sequence=(index + 1) * 2,
                    event_json="{}",
                    size_bytes=2,
                )
                for index in range(10_000)
            ]
        )
        with operator._lock:
            operator._logs[run.run_id] = logs
            operator._agent_events[(run.run_id, "agent_1")] = events

        log_page = operator.list_logs(
            run.run_id,
            after_sequence=9_000,
            page_size=10,
        )
        event_page = operator.list_agent_events(
            run.run_id,
            "agent_1",
            after_event_sequence=18_000,
            page_size=10,
        )

        assert len(log_page.logs) == 10
        assert [(item.start, item.stop) for item in logs.slices] == [(9_000, 9_011)]
        assert len(event_page.events) == 10
        assert [(item.start, item.stop) for item in events.slices] == [(9_000, 9_011)]
        assert events.index_reads < 32
    finally:
        operator.close()


def test_agent_event_append_never_materializes_cumulative_json(monkeypatch):
    import runtime.operator.operator as operator_module

    operator = Operator(watch=False, schedule=False)
    original_loads = operator_module.json.loads
    original_dumps = operator_module.json.dumps
    serialized_values = []

    def loads_forbidden(*args, **kwargs):
        raise AssertionError("append parsed cumulative compatibility JSON")

    def recording_dumps(value, *args, **kwargs):
        serialized_values.append(value)
        if isinstance(value, dict) and "events" in value:
            raise AssertionError("append serialized a cumulative event envelope")
        return original_dumps(value, *args, **kwargs)

    try:
        run = _add_run(operator, "run-append-bound")
        monkeypatch.setattr(operator_module.json, "loads", loads_forbidden)
        monkeypatch.setattr(operator_module.json, "dumps", recording_dumps)
        for sequence in range(1, 501):
            assert (
                operator._apply_event(run.run_id, _event_handle(), _evidence(sequence)) is False
            )

        assert len(operator._agent_events[(run.run_id, "agent_1")]) == 500
        assert run.nodes["agent_1"].agent_trace_json is None
        assert operator._trace_descriptors[(run.run_id, "agent_1")].event_count == 500
        assert not any(
            isinstance(value, dict) and "events" in value for value in serialized_values
        )

        monkeypatch.setattr(operator_module.json, "loads", original_loads)
        monkeypatch.setattr(operator_module.json, "dumps", original_dumps)
        materialized = operator.get_run(run.run_id)
        assert materialized is not None
        envelope = json.loads(materialized.nodes["agent_1"].agent_trace_json)
        assert len(envelope["events"]) == 500
    finally:
        operator.close()
