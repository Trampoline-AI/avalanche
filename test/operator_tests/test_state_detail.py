"""Snapshot isolation and concurrent detail lifecycle invariants."""

import json
import logging
import queue
import socket
import threading
from types import SimpleNamespace

import grpc
import pytest
from pydantic import BaseModel

from avalanche.agent.live import (
    AgentLiveEvent,
    AgentTraceFinished,
    AgentTraceUnavailable,
    CodeGenerated,
)
from avalanche.agent.native.predict_rlm import Correlated, RunStarted
from avalanche.agent.trace import AgentTrace
from runtime.operator import Operator
from runtime.operator import operator as operator_module
from runtime.operator.models import (
    LogDetailAppended,
    NodeState,
    RunState,
    RunStatus,
    RunStatusChanged,
)
from runtime.operator.operator import _CoordinatorProtocolError
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc
from runtime.operator.server import serve


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


def _live(
    sequence: int, *, invocation_id: str = "test-invocation", code: str = "pass"
) -> AgentLiveEvent:
    return AgentLiveEvent(
        invocation_id=invocation_id,
        sequence=sequence,
        timestamp_ns=sequence,
        runtime_kind="predict_rlm",
        common=CodeGenerated(turn=sequence, code=code),
        native=Correlated(kind="code.generated"),
    )


def _record(*, model: str = "test-model", run_id: str = "sdk-run") -> AgentTrace:
    """A final trace as the PredictRLM adapter builds it."""
    from predict_rlm import RunEvidence, RunTrace

    from avalanche.runtimes.predict_rlm.trace import trace_record

    record = trace_record(
        RunTrace(
            status="completed", model=model, iterations=0, max_iterations=3, duration_ms=1
        ),
        RunEvidence(run_id=run_id, complete=True, terminal_outcome="completed"),
        model=None,
        sub_model=None,
    )
    return record.model_copy(update={"turns_streamed": True})


def _finished(
    *, invocation_id: str = "test-invocation", complete: bool = True, **record: str
) -> AgentTraceFinished:
    return AgentTraceFinished(
        invocation_id=invocation_id, record=_record(**record), complete=complete
    )


def _unavailable(
    *,
    invocation_id: str = "test-invocation",
    status: str = "failed",
    complete: bool = True,
    error: str = "Stopped before the first iteration",
    external_run_id: str = "sdk-pre-trace",
) -> AgentTraceUnavailable:
    return AgentTraceUnavailable(
        invocation_id=invocation_id,
        runtime_kind="predict_rlm",
        status=status,
        error=error,
        external_run_id=external_run_id,
        complete=complete,
    )


def _wire(evidence: BaseModel | str) -> dict:
    body = evidence if isinstance(evidence, str) else evidence.model_dump_json()
    return {"type": "agent_evidence", "node_id": "agent_1", "event": body}


def _evidence(sequence: int) -> dict:
    return _wire(_live(sequence))


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
            bodies.append(operator.read_detail(descriptor.body_token))
        assert b"after-snapshot" not in bodies
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
            _wire(_finished(model=marker, run_id=f"sdk-{marker}")),
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
            list(stub.ReadActivityDetail(pb.ReadActivityDetailRequestV2(detail_ref=stale_ref)))
        assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        snapshot = stub.GetRunSnapshot(pb.GetRunSnapshotRequestV2(run_id="run-reused"))
        detail_ref = snapshot.nodes[0].trace.detail_ref
        chunks = list(
            stub.ReadActivityDetail(pb.ReadActivityDetailRequestV2(detail_ref=detail_ref))
        )
        trace = json.loads(b"".join(chunk.data for chunk in chunks))
        assert trace["record"]["header"]["model"] == "second"
        assert trace["record"]["header"]["external_run_id"] == "sdk-second"
    finally:
        if channel is not None:
            channel.close()
        if server is not None:
            server.stop(grace=0).wait()
        first.close()
        second.close()


@pytest.mark.parametrize(("status", "complete"), [("failed", True), ("cancelled", False)])
def test_unavailable_trace_retains_pre_trace_evidence(status, complete):
    operator = Operator(watch=False, schedule=False)
    run = _add_run(operator, "run-pre-trace")
    unavailable = _unavailable(
        invocation_id="pre-trace-invocation", status=status, complete=complete
    )
    try:
        operator._apply_event(run.run_id, _event_handle(), _wire(unavailable))
        materialized = operator.get_run(run.run_id)
        descriptor = materialized.nodes["agent_1"].trace
        assert descriptor.status == "unavailable"
        assert descriptor.available is True
        assert descriptor.complete is complete
        retained = operator.read_trace(
            run.run_id, "agent_1", operator_instance_id=operator.operator_instance_id
        )
        assert descriptor.size_bytes == len(retained.data)
        assert AgentTraceUnavailable.model_validate_json(retained.data) == unavailable
        envelope = json.loads(materialized.nodes["agent_1"].agent_trace_json)
        assert envelope["invocation_id"] == "pre-trace-invocation"
        assert envelope["terminal"]["external_run_id"] == "sdk-pre-trace"
        assert envelope["error"] == "Stopped before the first iteration"
    finally:
        operator.close()


def test_a_repeated_final_trace_is_ignored_and_a_different_one_is_a_protocol_error():
    operator = Operator(watch=False, schedule=False)
    run = _add_run(operator, "run-final-once")
    try:
        operator._apply_event(run.run_id, _event_handle(), _wire(_finished(complete=False)))
        original = operator.read_trace(
            run.run_id, "agent_1", operator_instance_id=operator.operator_instance_id
        )
        # The same final body again changes nothing, so replays are harmless.
        operator._apply_event(run.run_id, _event_handle(), _wire(_finished(complete=False)))
        assert operator.get_run(run.run_id).nodes["agent_1"].trace.revision == original.revision
        with pytest.raises(_CoordinatorProtocolError, match="second final trace"):
            operator._apply_event(run.run_id, _event_handle(), _wire(_finished(complete=True)))
        with pytest.raises(_CoordinatorProtocolError, match="after its final trace"):
            operator._apply_event(run.run_id, _event_handle(), _evidence(1))
        assert operator.get_run(run.run_id).nodes["agent_1"].trace.complete is False
    finally:
        operator.close()


@pytest.mark.parametrize("kind", ["trace_finished", "trace_unavailable"])
def test_terminal_detail_byte_limit_counts_the_whole_body(kind):
    terminal = _finished() if kind == "trace_finished" else _unavailable()
    limit = len(terminal.model_dump_json().encode()) - 1
    operator = Operator(watch=False, schedule=False, max_trace_body_bytes=limit)
    run = _add_run(operator, "run-terminal-limit")
    try:
        with pytest.raises(_CoordinatorProtocolError, match="exceeds"):
            operator._apply_event(run.run_id, _event_handle(), _wire(terminal))
        assert operator.get_run(run.run_id).nodes["agent_1"].trace is None
    finally:
        operator.close()


_TEXT_MARKER = "TEXT-MARKER"


@pytest.fixture(
    params=[
        "lifecycle-input",
        "terminal-model",
        "unavailable-run",
        "unavailable-error",
        "terminal-invocation",
        "unavailable-invocation",
    ],
)
def agent_text_event(request):
    """An evidence body whose one text field holds the given text."""
    builders = {
        "lifecycle-input": lambda: AgentLiveEvent(
            invocation_id="test-invocation",
            sequence=1,
            timestamp_ns=1,
            runtime_kind="predict_rlm",
            common=None,
            native=RunStarted(input_fields=("input",), inputs={"input": _TEXT_MARKER}),
        ),
        "terminal-model": lambda: _finished(model=_TEXT_MARKER),
        "unavailable-run": lambda: _unavailable(external_run_id=_TEXT_MARKER),
        "unavailable-error": lambda: _unavailable(error=_TEXT_MARKER),
        "terminal-invocation": lambda: _finished(invocation_id=_TEXT_MARKER),
        "unavailable-invocation": lambda: _unavailable(invocation_id=_TEXT_MARKER),
    }
    build_body = builders[request.param]

    def build(text):
        # JSON escapes keep text that cannot be encoded, such as a lone surrogate.
        body = build_body().model_dump_json()
        return _wire(body.replace(_TEXT_MARKER, json.dumps(text)[1:-1]))

    return build


def test_unencodable_agent_detail_is_a_protocol_fault(agent_text_event):
    operator = Operator(watch=False, schedule=False)
    run = _add_run(operator, "run-unencodable")
    try:
        with pytest.raises(_CoordinatorProtocolError) as error:
            operator._apply_event(
                run.run_id, _event_handle(), agent_text_event("private-input-\ud800")
            )
        assert str(error.value).isascii()
        assert "private-input" not in str(error.value)
        snapshot = operator.get_latest_run_snapshot(
            run.run_id, operator_instance_id=operator.operator_instance_id
        )
        assert snapshot.nodes[0].trace is None
        assert snapshot.latest_log_sequence == 0
    finally:
        operator.close()


def test_unencodable_agent_detail_drain_terminalizes_run(agent_text_event, monkeypatch):
    class EventQueue(queue.Queue):
        def close(self):
            pass

    operator = Operator(watch=False, schedule=False)
    run = _add_run(operator, "run-unencodable-drain")
    handle = _event_handle()
    handle.result_bundle = operator._result_store.prepare()
    handle.event_queue = EventQueue()
    handle.process = SimpleNamespace(is_alive=lambda: False)
    handle.windows_job = None
    handle.publication_event = threading.Event()
    handle.publication_event.set()
    monkeypatch.setattr(operator_module, "_teardown_process_group", lambda *_a, **_k: True)
    operator._active_runs[run.run_id] = handle
    try:
        operator._drain_run_events(
            run.run_id,
            handle,
            [
                {"type": "running", "timestamp": 1.0},
                agent_text_event("private-input-\ud800"),
            ],
        )
        snapshot = operator.get_latest_run_snapshot(
            run.run_id, operator_instance_id=operator.operator_instance_id
        )
        assert snapshot.summary.status is RunStatus.FAILED
        assert snapshot.terminal_seal.terminal_status is RunStatus.FAILED
        assert run.ended_at is not None
        assert run.run_id not in operator._active_runs
        assert snapshot.nodes[0].trace is None
        logs = operator.list_logs(page_token=snapshot.log_page_token)
        assert len(logs.logs) == 1
        message = operator.read_detail(logs.logs[0].body_token).decode("utf-8")
        assert "private-input" not in message
        assert "\ud800" not in message
    finally:
        operator.close()


def test_agent_detail_retains_valid_unicode(agent_text_event):
    operator = Operator(watch=False, schedule=False)
    run = _add_run(operator, "run-unicode")
    text = "日本語 café \U0001d11e"
    event = agent_text_event(text)
    try:
        operator._apply_event(run.run_id, _event_handle(), event)
        materialized = operator.get_run(run.run_id)
        envelope = materialized.nodes["agent_1"].agent_trace_json
        assert text in json.dumps(json.loads(envelope), ensure_ascii=False)
        if json.loads(event["event"])["kind"] != "event":
            retained = operator.read_trace(
                run.run_id, "agent_1", operator_instance_id=operator.operator_instance_id
            )
            assert materialized.nodes["agent_1"].trace.size_bytes == len(retained.data)
            assert text in retained.data.decode("utf-8")
    finally:
        operator.close()


def _v1_evidence() -> str:
    return json.dumps(
        {
            "kind": "evidence",
            "invocation_id": "test-invocation",
            "sequence": 1,
            "event_kind": "code.generated",
            "timestamp_ns": 1,
            "data": {"iteration": 1, "code": "pass"},
        }
    )


def _patched(evidence: BaseModel, path: tuple[str, ...], value: object) -> str:
    body = json.loads(evidence.model_dump_json())
    target = body
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    return json.dumps(body)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_v1_evidence, id="v1-body"),
        pytest.param(
            lambda: _patched(_live(1), ("schema_version",), 1), id="v1-schema-version"
        ),
        pytest.param(
            lambda: _patched(_live(1), ("native", "format"), "other.run/v1"),
            id="unknown-native-format",
        ),
        pytest.param(
            lambda: _patched(_live(1), ("native", "kind"), "code.compiled"),
            id="unknown-native-kind",
        ),
        pytest.param(
            lambda: _patched(_live(1), ("runtime_kind",), "other_runtime"),
            id="unknown-runtime",
        ),
        pytest.param(
            lambda: _patched(_live(1), ("common",), None),
            id="native-kind-without-its-common-event",
        ),
        pytest.param(
            lambda: _patched(
                _live(1),
                ("native",),
                {"format": "predict_rlm.run/v1", "kind": "tool.started"},
            ),
            id="native-kind-with-another-common-event",
        ),
        pytest.param(
            lambda: _patched(_finished(), ("record", "turns_streamed"), False),
            id="final-trace-with-unstreamed-turns",
        ),
        pytest.param(
            lambda: _patched(_finished(), ("record", "header", "trace_format"), "other/v1"),
            id="header-format-mismatch",
        ),
        pytest.param(
            lambda: _patched(_unavailable(), ("status",), "completed"),
            id="unknown-status",
        ),
        pytest.param(lambda: _patched(_unavailable(), ("extra",), 1), id="extra-field"),
        pytest.param(lambda: "{", id="not-json"),
    ],
)
def test_malformed_agent_evidence_is_a_protocol_error(body):
    operator = Operator(watch=False, schedule=False)
    run = _add_run(operator, "run-malformed-evidence")
    try:
        with pytest.raises(_CoordinatorProtocolError, match="invalid agent evidence body"):
            operator._apply_event(run.run_id, _event_handle(), _wire(body()))
        assert operator.get_run(run.run_id).nodes["agent_1"].trace is None
    finally:
        operator.close()


def test_agent_evidence_must_be_a_json_string():
    operator = Operator(watch=False, schedule=False)
    run = _add_run(operator, "run-dict-evidence")
    try:
        with pytest.raises(_CoordinatorProtocolError, match="JSON string"):
            operator._apply_event(
                run.run_id,
                _event_handle(),
                {"type": "agent_evidence", "node_id": "agent_1", "event": {"kind": "event"}},
            )
    finally:
        operator.close()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_agent_event_publication_failure_reaches_the_workflow(asynchronous):
    from avalanche._agent_evidence import emit_agent_evidence
    from runtime.operator.run_worker import _with_agent_evidence

    class BrokenQueue:
        def put(self, event):
            raise OSError("agent event transport disconnected")

    event = _unavailable()

    def run_agent():
        emit_agent_evidence(event)

    async def run_async_agent():
        emit_agent_evidence(event)

    fn = run_async_agent if asynchronous else run_agent
    wrapped = _with_agent_evidence("agent_1", fn, BrokenQueue())
    with pytest.raises(OSError, match="agent event transport disconnected"):
        if asynchronous:
            import asyncio

            asyncio.run(wrapped())
        else:
            wrapped()


@pytest.mark.parametrize("failed", [False, True])
def test_empty_exception_message_remains_a_failure_not_a_success(failed):
    operator = Operator(watch=False, schedule=False)
    run = _add_run(operator, "run-empty-exception")
    from avalanche.agent.live import ModelFinished

    event = AgentLiveEvent(
        invocation_id="test-invocation",
        sequence=1,
        timestamp_ns=1,
        runtime_kind="predict_rlm",
        common=ModelFinished(call_id="call", error="" if failed else None),
        native=Correlated(kind="predict.finished"),
    )
    try:
        operator._record_agent_evidence_event(run, "agent_1", event.model_dump_json())
        envelope = json.loads(operator.get_run(run.run_id).nodes["agent_1"].agent_trace_json)
        assert envelope["status"] == ("error" if failed else "in_progress")
        assert envelope["error"] == ("" if failed else None)
        snapshot = operator.get_latest_run_snapshot(
            run.run_id, operator_instance_id=operator.operator_instance_id
        )
        page = operator.list_agent_events(page_token=snapshot.nodes[0].event_page_token)
        assert page.events[0].error is failed
    finally:
        operator.close()
