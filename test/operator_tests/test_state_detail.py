import json
import logging
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest

from avalanche.operator import Operator
from avalanche.operator.models import NodeState, RunState, RunStatus
from avalanche.operator.server import TRACE_CHUNK_BYTES, serve
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc
from runtime.operator.scheduler import Scheduler


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
    baseline = {
        thread
        for thread in threading.enumerate()
        if thread.name in thread_names
    }

    for _ in range(3):
        with pytest.raises(RuntimeError, match="scheduler startup failed"):
            Operator(
                workflow_paths=[str(fixture)],
                watch=True,
                schedule=True,
            )

        leaked = {
            thread
            for thread in threading.enumerate()
            if thread.name in thread_names
        } - baseline
        assert leaked == set()


def test_structural_snapshot_excludes_detail_bodies_while_legacy_state_remains():
    operator = Operator(watch=False, schedule=False)
    try:
        run = _add_run(operator, "run-1")
        operator._apply_event(
            run.run_id,
            {
                "type": "log",
                "timestamp": 1.0,
                "level": logging.INFO,
                "node_id": "agent_1",
                "message": "hello",
            },
        )
        operator._apply_event(run.run_id, _evidence(1))
        operator._apply_event(
            run.run_id,
            {
                "type": "agent_evidence",
                "node_id": "agent_1",
                "event": {
                    "kind": "trace_finished",
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

        legacy = operator.get_run(run.run_id)
        assert legacy is not None
        assert [entry.message for entry in legacy.logs] == [
            "hello",
            "Agent code.generated iteration=1",
            "Agent trace completed",
        ]
        assert json.loads(legacy.nodes["agent_1"].agent_trace_json)["trace"][
            "status"
        ] == "completed"
    finally:
        operator.close()


def test_log_and_agent_event_pagination_use_exclusive_deduplicating_cursors():
    operator = Operator(watch=False, schedule=False)
    try:
        run = _add_run(operator, "run-1")
        for index in range(3):
            operator._apply_event(
                run.run_id,
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
            run.run_id,
            after_sequence=first_logs.next_sequence,
            page_size=2,
        )
        assert [item.sequence for item in first_logs.logs] == [1, 2]
        assert first_logs.has_more is True
        assert [item.sequence for item in second_logs.logs] == [3]
        assert second_logs.has_more is False

        operator._apply_event(run.run_id, _evidence(1))
        operator._apply_event(run.run_id, _evidence(1))
        operator._apply_event(run.run_id, _evidence(2))
        first_events = operator.list_agent_events(run.run_id, "agent_1", page_size=1)
        second_events = operator.list_agent_events(
            run.run_id,
            "agent_1",
            after_event_sequence=first_events.next_event_sequence,
            page_size=10,
        )
        assert [item.event_sequence for item in first_events.events] == [1]
        assert first_events.has_more is True
        assert [item.event_sequence for item in second_events.events] == [2]
        assert second_events.has_more is False
    finally:
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
            runs["run-1"].run_id,
            {"type": "running", "timestamp": 1.0},
        )
        for event_sequence in range(1, 6):
            operator._apply_event(runs["run-1"].run_id, _evidence(event_sequence))
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
            first_run.run_id,
            {"type": "running", "timestamp": 1.0},
        )
        current = operator.list_run_summaries(page_size=10)
        assert current.as_of_sequence > evicted.as_of_sequence

        port = _unused_port()
        server = serve(operator, port=port, block=False)
        channel = grpc.insecure_channel(f"localhost:{port}")
        grpc.channel_ready_future(channel).result(timeout=5)
        stub = pb_grpc.OperatorServiceStub(channel)

        with pytest.raises(grpc.RpcError) as snapshot_error:
            stub.GetRunSnapshot(
                pb.GetRunSnapshotRequest(
                    run_id=first_run.run_id,
                    operator_instance_id=evicted.operator_instance_id,
                    as_of_sequence=evicted.as_of_sequence,
                )
            )
        assert snapshot_error.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        with pytest.raises(grpc.RpcError) as page_error:
            stub.ListRunSummaries(
                pb.ListRunSummariesRequest(
                    page_size=1,
                    page_token=evicted.next_page_token,
                )
            )
        assert page_error.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        snapshot = stub.GetRunSnapshot(
            pb.GetRunSnapshotRequest(
                run_id=first_run.run_id,
                operator_instance_id=current.operator_instance_id,
                as_of_sequence=current.as_of_sequence,
            )
        )
        assert snapshot.as_of_sequence == current.as_of_sequence
        assert snapshot.summary.status == RunStatus.RUNNING.value
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
    subscription = operator.subscribe()
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
        first_sequence, first_run = subscription.get(timeout=5)
        assert first_run.status == RunStatus.PENDING

        page = page_holder[0]
        summary = next(item for item in page.runs if item.run_id == "run-publishing")
        assert summary.created_sequence > 0
        assert first_sequence == summary.created_sequence
        assert summary.revision >= summary.created_sequence
        assert page.as_of_sequence >= summary.revision
    finally:
        operator.publish_release.set()
        starter.join(timeout=1)
        if reader.ident is not None:
            reader.join(timeout=1)
        operator.unsubscribe(subscription)
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
            operator._apply_event(run.run_id, _evidence(1))
        except BaseException as exc:
            apply_errors.append(exc)

    def read_detail() -> None:
        page = operator.list_run_summaries()
        observed["snapshot"] = operator.get_run_snapshot(
            run.run_id,
            operator_instance_id=page.operator_instance_id,
            as_of_sequence=page.as_of_sequence,
        )
        observed["logs"] = operator.list_logs(run.run_id)
        observed["events"] = operator.list_agent_events(run.run_id, "agent_1")
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
        assert snapshot.latest_log_sequence == logs.next_sequence == 1
        assert events.next_event_sequence == 1
    finally:
        operator.publish_release.set()
        publisher.join(timeout=1)
        if reader.ident is not None:
            reader.join(timeout=1)
        operator.close()


def test_concurrent_publishers_dispatch_callbacks_and_subscribers_in_sequence_order():
    operator = _OrderedDeliveryOperator(watch=False, schedule=False)
    run = _add_run(operator, "run-ordered")
    subscription = operator.subscribe(operator.current_sequence)
    first_callback_entered = threading.Event()
    release_first_callback = threading.Event()
    callback_messages = []
    log_callback_messages = []
    publisher_errors = []

    def on_run(snapshot: RunState) -> None:
        message = snapshot.logs[-1].message
        if message == "N":
            first_callback_entered.set()
            if not release_first_callback.wait(timeout=5):
                raise TimeoutError("Timed out waiting to release first callback")
        callback_messages.append(message)

    def on_log(entry) -> None:
        log_callback_messages.append(entry.message)

    def publish(message: str, timestamp: float) -> None:
        try:
            operator._apply_event(
                run.run_id,
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

    operator.on_run_update(on_run)
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
        assert [sequence for sequence, _ in subscriber_updates] == [
            first_sequence,
            first_sequence + 1,
        ]
        assert [snapshot.logs[-1].message for _, snapshot in subscriber_updates] == [
            "N",
            "N+1",
        ]
        assert callback_messages == ["N", "N+1"]
        assert log_callback_messages == ["N", "N+1"]
    finally:
        release_first_callback.set()
        if publisher_n.ident is not None:
            publisher_n.join(timeout=1)
        if publisher_n1.ident is not None:
            publisher_n1.join(timeout=1)
        operator.unsubscribe(subscription)
        operator.close()
    assert not operator._notification_thread.is_alive()


def test_close_keeps_dispatcher_alive_for_notification_from_delayed_drain():
    operator = Operator(watch=False, schedule=False, cancel_grace=0)
    run = _add_run(operator, "run-delayed-close")
    subscription = operator.subscribe(operator.current_sequence)
    callback_statuses = []
    drain_entered = threading.Event()
    release_drain = threading.Event()

    def delayed_drain() -> None:
        drain_entered.set()
        if not release_drain.wait(timeout=10):
            raise TimeoutError("Timed out waiting to release delayed drain")
        operator._apply_event(
            run.run_id,
            {"type": "terminal", "status": "cancelled"},
        )

    operator.on_run_update(lambda snapshot: callback_statuses.append(snapshot.status))
    drain = threading.Thread(target=delayed_drain)
    handle = SimpleNamespace(
        process=SimpleNamespace(pid=None),
        cancel_event=threading.Event(),
        start_event=threading.Event(),
        windows_job=None,
        drain_thread=drain,
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
        _, final_snapshot = subscription.get(timeout=5)
        assert final_snapshot.status == RunStatus.CANCELLED
        assert callback_statuses == [RunStatus.CANCELLED]
    finally:
        release_drain.set()
        if drain.ident is not None:
            drain.join(timeout=1)
        operator.unsubscribe(subscription)
        operator.close()


def test_read_trace_streams_more_than_four_mib_in_bounded_revisioned_chunks():
    operator = Operator(watch=False, schedule=False)
    server = None
    channel = None
    try:
        run = _add_run(operator, "run-large")
        operator._apply_event(run.run_id, _evidence(1))
        operator._apply_event(
            run.run_id,
            {
                "type": "agent_evidence",
                "node_id": "agent_1",
                "event": {
                    "kind": "trace_finished",
                    "trace": {
                        "status": "completed",
                        "evidence": {"complete": True},
                        "payload": "x" * (4 * 1024 * 1024 + 17),
                    },
                },
            },
        )
        expected = operator.read_trace(run.run_id, "agent_1")

        port = _unused_port()
        server = serve(operator, port=port, block=False)
        channel = grpc.insecure_channel(f"localhost:{port}")
        grpc.channel_ready_future(channel).result(timeout=5)
        stub = pb_grpc.OperatorServiceStub(channel)
        summaries = stub.ListRunSummaries(pb.ListRunSummariesRequest(page_size=10))
        snapshot = stub.GetRunSnapshot(
            pb.GetRunSnapshotRequest(
                run_id=run.run_id,
                operator_instance_id=summaries.operator_instance_id,
                as_of_sequence=summaries.as_of_sequence,
            )
        )
        logs = stub.ListLogs(pb.ListLogsRequest(run_id=run.run_id, page_size=1))
        events = stub.ListAgentEvents(
            pb.ListAgentEventsRequest(
                run_id=run.run_id,
                node_id="agent_1",
                page_size=10,
            )
        )
        chunks = list(
            stub.ReadTrace(
                pb.ReadTraceRequest(
                    run_id=run.run_id,
                    node_id="agent_1",
                    revision=expected.revision,
                )
            )
        )

        assert summaries.operator_instance_id == operator.operator_instance_id
        assert [item.run_id for item in summaries.runs] == [run.run_id]
        assert snapshot.operator_instance_id == operator.operator_instance_id
        assert snapshot.nodes[0].trace.revision == expected.revision
        assert [item.sequence for item in logs.logs] == [1]
        assert logs.has_more is True
        assert [item.event_sequence for item in events.events] == [1]
        assert len(expected.data) > 4 * 1024 * 1024
        assert len(chunks) > 4
        assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
        assert all(0 < len(chunk.data) <= TRACE_CHUNK_BYTES for chunk in chunks)
        assert all(chunk.revision == expected.revision for chunk in chunks)
        assert [chunk.eof for chunk in chunks] == [False] * (len(chunks) - 1) + [True]
        assert b"".join(chunk.data for chunk in chunks) == expected.data
    finally:
        if channel is not None:
            channel.close()
        if server is not None:
            server.stop(grace=0).wait()
        operator.close()
