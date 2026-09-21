"""Classifier activity stays identity-pinned across paging, replay, and completion."""

import json
import socket
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Literal

import grpc
import pytest

import avalanche as ava
from avalanche.classifier.models import (
    ChoiceAnswer,
    ChoiceQuestion,
    ClassificationResult,
    ClassificationUsage,
    ClassifierDeclaration,
    ClassifierInvocation,
    ClassifierRuntime,
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
)
from avalanche.step_interface import StepInterface, StepOutput
from runtime.operator.client import GrpcStateProvider, StreamState
from runtime.operator.convert_v2 import (
    classifier_event_descriptor_from_v2,
    workflow_topology_from_v2,
)
from runtime.operator.models import (
    ClassifierAnswerSummary,
    ClassifierChoiceSummary,
    ClassifierEventDetailAppended,
    ClassifierNoulSummary,
    ClassifierScoreSummary,
    RunStatus,
)
from runtime.operator.operator import (
    Operator,
    _CoordinatorProtocolError,
    _validate_preparation_event,
)
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc
from runtime.operator.run_worker import _workflow_metadata
from runtime.operator.server import serve


@contextmanager
def _serve(operator: Operator):
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        port = sock.getsockname()[1]
    server = serve(operator, port=port, block=False)
    address = f"localhost:{port}"
    channel = grpc.insecure_channel(address)
    try:
        grpc.channel_ready_future(channel).result(timeout=5)
        yield pb_grpc.OperatorServiceV2Stub(channel), address
    finally:
        channel.close()
        server.stop(grace=0).wait()


@pytest.fixture
def transport():
    operator = Operator([], schedule=False, watch=False, stream_history_capacity=100)
    try:
        with _serve(operator) as (service, address):
            yield operator, service, address
    finally:
        operator.close()


def _declaration(choice: str = "accept") -> ClassifierDeclaration:
    return ClassifierDeclaration(
        questions={
            "route": ChoiceQuestion(
                type="choice", criteria={choice: "Accept the request", "reject": "Reject it"}
            ),
            "match": NoulQuestion(type="noul", instructions="Original retained question"),
            "priority": ScoreQuestion(type="score", criteria=["Routine", "Urgent"]),
            **{
                f"extra-{index}": NoulQuestion(type="noul", instructions=f"Check {index}")
                for index in range(6)
            },
        },
        runtime=ClassifierRuntime(model="jev-latest", timeout=10.0),
    )


def _event_handle(operator: Operator | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        cancel_event=threading.Event(),
        result_bundle=operator._result_store.prepare() if operator is not None else None,
        success_quiesced=False,
    )


def _seed_run(
    operator: Operator, run_id: str, declaration: ClassifierDeclaration | None = None
):
    if declaration is None:
        declaration = _declaration()
    declaration_json = declaration.model_dump_json()
    node_ids = ["classify", "other", "agent"]
    run = operator._run_from_prepared(
        run_id,
        "removed-flow.py::flow",
        "Retained flow",
        "manual",
        100.0,
        {
            "type": "prepared",
            "display_name": "Retained flow",
            "node_ids": node_ids,
            "graph": {node_id: [] for node_id in node_ids},
            "node_types": {node_id: "step" for node_id in node_ids},
            "display_names": {node_id: node_id for node_id in node_ids},
            "agent_field_schemas_json": {},
            "agent_instruction_lines": {},
            "standard_step_docstring_lines": {},
            "classifier_metadata_json": {
                node_id: declaration_json for node_id in ("classify", "other")
            },
            "step_interface_json": {
                node_id: StepInterface(
                    step_inputs=[], step_output=StepOutput()
                ).model_dump_json()
                for node_id in node_ids
            },
        },
    )
    with operator._lock:
        operator._runs[run_id] = run
    operator._notify_run(run)
    return run


def _invocation(
    index: int,
    status: Literal["running", "success", "failed", "cancelled"],
    *,
    choice: str = "accept",
) -> ClassifierInvocation:
    return ClassifierInvocation(
        invocation_id=f"call-{index}",
        invocation_index=index,
        status=status,
        started_at=100.0 + index,
        ended_at=None if status == "running" else 101.0 + index,
        declaration=_declaration(choice),
        input={"index": index, "ticket": ["captured state"]},
        result=(
            ClassificationResult(
                model="jev-latest",
                answers={
                    "route": ChoiceAnswer(
                        type="choice",
                        choice=choice,
                        probabilities={choice: 1.0, "reject": 0.0},
                        confidence=1.0,
                    ),
                    "match": NoulAnswer(type="noul", noul=0.0),
                    "priority": ScoreAnswer(
                        type="score",
                        score=0.0,
                        legend={"0": "Routine", "1": "Urgent"},
                        probabilities={"0": 1.0, "1": 0.0},
                        confidence=1.0,
                    ),
                    **{
                        f"extra-{index}": NoulAnswer(type="noul", noul=1.0)
                        for index in range(6)
                    },
                },
                usage=ClassificationUsage(input_tokens=2, output_tokens=1),
            )
            if status == "success"
            else None
        ),
        error="Request interrupted" if status in {"failed", "cancelled"} else None,
    )


def _answers(choice: str = "accept") -> tuple[ClassifierAnswerSummary, ...]:
    return (
        ClassifierChoiceSummary("route", choice),
        ClassifierNoulSummary("match", 0.0),
        ClassifierScoreSummary("priority", 0.0),
        *(ClassifierNoulSummary(f"extra-{index}", 1.0) for index in range(6)),
    )


def _publish(operator: Operator, run_id: str, invocation: ClassifierInvocation) -> None:
    operator._apply_event(
        run_id,
        _event_handle(),
        {
            "type": "classifier_evidence",
            "node_id": "classify",
            "event": invocation.model_dump(mode="json"),
        },
    )


def _read_invocation(service, reference) -> ClassifierInvocation:
    chunks = service.ReadActivityDetail(
        pb.ReadActivityDetailRequestV2(detail_ref=reference), timeout=5
    )
    return ClassifierInvocation.model_validate_json(b"".join(chunk.data for chunk in chunks))


def _page_history(service, run_id: str, continuation, order: int):
    activities = []
    seen = set()
    while True:
        assert continuation.continuation_id not in seen
        seen.add(continuation.continuation_id)
        page = service.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id=run_id,
                node_id="classify",
                page_size=1,
                continuation=continuation,
                order=order,
            ),
            timeout=5,
        )
        activities.extend(page.activities)
        if not page.next_page.continuation_id:
            return activities
        continuation = page.next_page


def test_snapshot_paging_retains_classifier_records_without_a_current_workflow(transport):
    operator, service, _ = transport
    run = _seed_run(operator, "retained")
    records = [_invocation(0, "running"), _invocation(0, "success"), _invocation(1, "running")]
    for record in records:
        _publish(operator, run.run_id, record)
    snapshot = service.GetRunSnapshot(pb.GetRunSnapshotRequestV2(run_id=run.run_id))
    continuation = next(
        node for node in snapshot.nodes if node.node_id == "classify"
    ).activity_continuation

    _publish(operator, run.run_id, _invocation(1, "failed"))
    operator._apply_event(
        run.run_id,
        _event_handle(operator),
        {"type": "terminal", "status": "failed", "error": "Postprocessing failed"},
    )
    assert not service.DiscoverFlows(pb.DiscoverFlowsRequestV2()).flows
    assert (
        ClassifierDeclaration.model_validate_json(
            snapshot.topology.classifier_metadata_json["classify"]
        )
        == _declaration()
    )

    forward = _page_history(service, run.run_id, continuation, pb.PAGE_ORDER_V2_FORWARD)
    reverse = _page_history(service, run.run_id, continuation, pb.PAGE_ORDER_V2_NEWEST_FIRST)
    assert [item.run_sequence for item in forward] == [1, 2, 3]
    assert [item.run_sequence for item in reverse] == [3, 2, 1]
    assert [_read_invocation(service, item.detail_ref) for item in forward] == records
    assert [item.activity_id for item in forward] == [
        "classifier:classify:1",
        "classifier:classify:2",
        "classifier:classify:3",
    ]
    assert {item.kind for item in forward} == {"classifier_event"}
    summaries = [classifier_event_descriptor_from_v2(item) for item in forward]
    assert [item.invocation_index for item in summaries] == [0, 0, 1]
    assert [item.answers for item in summaries] == [(), _answers(), ()]
    assert [item.duration_ms for item in summaries] == [None, 1000, None]
    assert [
        answer.WhichOneof("answer") for answer in forward[1].classifier_summary.answers
    ] == [
        "choice",
        "noul",
        "score",
        *(["noul"] * 6),
    ]
    assert [item.detail_ref.activity_id for item in reverse] == [
        item.activity_id for item in reverse
    ]
    latest = service.ListRunActivity(
        pb.ListRunActivityRequestV2(run_id=run.run_id, node_id="classify")
    )
    assert [
        _read_invocation(service, item.detail_ref).status for item in latest.activities
    ] == ["running", "success", "running", "failed"]


def test_classifier_continuation_rejects_other_runs_nodes_categories_and_scopes(transport):
    operator, service, _ = transport
    _seed_run(operator, "run-a")
    _seed_run(operator, "run-b")
    _publish(operator, "run-a", _invocation(0, "running"))
    _publish(operator, "run-a", _invocation(0, "success"))
    first = service.ListRunActivity(
        pb.ListRunActivityRequestV2(run_id="run-a", node_id="classify", page_size=1)
    )
    for run_id, node_id in (
        ("run-b", "classify"),
        ("run-a", "other"),
        ("run-a", "agent"),
        ("run-a", ""),
    ):
        with pytest.raises(grpc.RpcError) as error:
            service.ListRunActivity(
                pb.ListRunActivityRequestV2(
                    run_id=run_id, node_id=node_id, continuation=first.next_page
                )
            )
        assert error.value.code() is grpc.StatusCode.FAILED_PRECONDITION

    foreign = pb.ContinuationRefV2()
    foreign.CopyFrom(first.next_page)
    foreign.scope_ref.reference = "another-operator"
    with pytest.raises(grpc.RpcError) as error:
        service.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id="run-a", node_id="classify", continuation=foreign
            )
        )
    assert error.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    second = service.ListRunActivity(
        pb.ListRunActivityRequestV2(
            run_id="run-a", node_id="classify", continuation=first.next_page
        )
    )
    assert _read_invocation(service, second.activities[0].detail_ref) == _invocation(
        0, "success"
    )


def test_classifier_detail_requires_issued_immutable_identity_and_survives_later_events(
    transport,
):
    operator, service, _ = transport
    _seed_run(operator, "detail-run")
    _publish(operator, "detail-run", _invocation(0, "running"))
    page = service.ListRunActivity(
        pb.ListRunActivityRequestV2(run_id="detail-run", node_id="classify")
    )
    reference = page.activities[0].detail_ref
    _publish(operator, "detail-run", _invocation(0, "success"))
    assert _read_invocation(service, reference) == _invocation(0, "running")

    forged = pb.ActivityDetailRefV2()
    forged.CopyFrom(reference)
    forged.activity_id = "agent:classify:1"
    with pytest.raises(grpc.RpcError) as error:
        _read_invocation(service, forged)
    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT

    with _serve(operator) as (fresh_view, _):
        with pytest.raises(grpc.RpcError) as error:
            _read_invocation(fresh_view, reference)
        assert error.value.code() is grpc.StatusCode.FAILED_PRECONDITION
        refreshed = fresh_view.ListRunActivity(
            pb.ListRunActivityRequestV2(run_id="detail-run", node_id="classify")
        )
        assert _read_invocation(fresh_view, refreshed.activities[0].detail_ref) == _invocation(
            0, "running"
        )


def test_live_classifier_details_advance_python_client_without_agent_traces(transport):
    operator, service, address = transport
    issued = service.ListRunSummaries(pb.ListRunSummariesRequestV2()).cursor
    provider = GrpcStateProvider(address)
    provider._install_structural_baseline(operator.operator_instance_id, issued.event_ulid, {})
    provider._event_cursor = issued
    details = []
    observed = threading.Event()
    failed = threading.Event()

    def on_detail(detail):
        if isinstance(detail, ClassifierEventDetailAppended):
            details.append(detail)
            if detail.event.event_kind == "success":
                observed.set()

    provider.on_detail_update(on_detail)
    provider.on_run_update(lambda run: failed.set() if run.status is RunStatus.FAILED else None)
    try:
        provider.start_stream()
        run = _seed_run(operator, "live-run")
        _publish(operator, run.run_id, _invocation(0, "running"))
        _publish(operator, run.run_id, _invocation(0, "success"))
        assert observed.wait(5)
        assert provider.stream_state is StreamState.LIVE
        assert [
            ClassifierInvocation.model_validate_json(detail.event.event_json)
            for detail in details
        ] == [_invocation(0, "running"), _invocation(0, "success")]
        assert [detail.event.invocation_index for detail in details] == [0, 0]
        assert [detail.event.answers for detail in details] == [(), _answers()]
        assert {detail.operator_instance_id for detail in details} == {
            operator.operator_instance_id
        }
        snapshot = service.GetRunSnapshot(pb.GetRunSnapshotRequestV2(run_id=run.run_id))
        assert provider._cursor.event_ulid == snapshot.cursor.event_ulid
        assert provider._event_cursor == snapshot.cursor

        # Run-created continuations are pinned before the first invocation, not
        # silently switched to agent paging or widened to the current history.
        created = provider._runs_by_id[run.run_id]
        continuation = provider._activity_continuation_for(
            created.nodes["classify"].event_page_token, run_id=run.run_id, node_id="classify"
        )
        empty_baseline = provider._stub.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id=run.run_id, node_id="classify", continuation=continuation
            )
        )
        assert not empty_baseline.activities

        operator._apply_event(
            run.run_id,
            _event_handle(operator),
            {"type": "terminal", "status": "failed", "error": "Postprocessing failed"},
        )
        assert failed.wait(5)
        retained = provider.get_run(run.run_id)
        assert retained is not None
        assert retained.status is RunStatus.FAILED
        assert dict(retained.topology.classifier_metadata_json) == dict(
            run.topology.classifier_metadata_json
        )
        assert retained.nodes["classify"].agent_trace_json is None
        assert retained.nodes["classify"].trace is None
    finally:
        provider.close()


def test_expired_classifier_details_preserve_paging_and_replay(transport, monkeypatch):
    operator, service, address = transport
    issued = service.ListRunSummaries(pb.ListRunSummariesRequestV2()).cursor
    run = _seed_run(operator, "expired-run")
    records = []
    for index, status in enumerate(("success", "failed", "cancelled")):
        records.extend((_invocation(index, "running"), _invocation(index, status)))
    for record in records:
        _publish(operator, run.run_id, record)
    snapshot = service.GetRunSnapshot(pb.GetRunSnapshotRequestV2(run_id=run.run_id))
    continuation = next(
        node for node in snapshot.nodes if node.node_id == "classify"
    ).activity_continuation
    original = _page_history(service, run.run_id, continuation, pb.PAGE_ORDER_V2_FORWARD)
    operator._apply_event(
        run.run_id,
        _event_handle(operator),
        {"type": "terminal", "status": "failed", "error": "Postprocessing failed"},
    )
    terminal = operator.get_run(run.run_id)
    assert terminal is not None and terminal.ended_at is not None
    assert operator._result_retention_seconds is not None
    expired_at = terminal.ended_at + operator._result_retention_seconds + 1
    with monkeypatch.context() as clock:
        clock.setattr(
            "runtime.operator.operator.time",
            SimpleNamespace(monotonic=lambda: expired_at),
        )
        operator._expire_results()

    forward = _page_history(service, run.run_id, continuation, pb.PAGE_ORDER_V2_FORWARD)
    reverse = _page_history(service, run.run_id, continuation, pb.PAGE_ORDER_V2_NEWEST_FIRST)
    expected = []
    for activity in original:
        descriptor = pb.RunActivityDescriptorV2()
        descriptor.CopyFrom(activity)
        descriptor.ClearField("detail_ref")
        expected.append(descriptor)
    assert forward == expected
    assert reverse == list(reversed(expected))
    assert [item.event_kind for item in forward] == [record.status for record in records]
    summaries = [classifier_event_descriptor_from_v2(item) for item in forward]
    assert [item.invocation_index for item in summaries] == [0, 0, 1, 1, 2, 2]
    assert [item.answers for item in summaries] == [(), _answers(), (), (), (), ()]
    assert [item.error for item in summaries] == [False, False, False, True, False, True]
    assert [item.duration_ms for item in summaries] == [None, 1000, None, 1000, None, 1000]
    assert all(
        not item.body_token
        for item in operator.list_classifier_events(run.run_id, "classify").events
    )
    for activity in original:
        with pytest.raises(grpc.RpcError) as error:
            _read_invocation(service, activity.detail_ref)
        assert error.value.code() is grpc.StatusCode.NOT_FOUND
    forged = pb.ActivityDetailRefV2()
    forged.CopyFrom(original[0].detail_ref)
    forged.activity_id = "classifier:other:1"
    with pytest.raises(grpc.RpcError) as error:
        _read_invocation(service, forged)
    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT

    latest = service.GetRunSnapshot(pb.GetRunSnapshotRequestV2(run_id=run.run_id))
    replay = service.WatchRunStatus(pb.WatchRunStatusRequestV2(after_cursor=issued), timeout=5)
    replayed = []
    try:
        for message in replay:
            if (
                message.WhichOneof("payload") == "activity_appended"
                and message.activity_appended.activity.kind == "classifier_event"
            ):
                replayed.append(message.activity_appended.activity)
            if message.cursor == latest.cursor:
                break
    finally:
        replay.cancel()
    assert replayed == expected

    provider = GrpcStateProvider(address)
    provider._install_structural_baseline(operator.operator_instance_id, issued.event_ulid, {})
    provider._event_cursor = issued
    details = []
    completed = threading.Event()
    provider.on_detail_update(
        lambda detail: details.append(detail)
        if isinstance(detail, ClassifierEventDetailAppended)
        else None
    )
    provider.on_run_update(
        lambda state: completed.set() if state.terminal_seal is not None else None
    )
    try:
        provider.start_stream()
        assert completed.wait(5)
        assert provider.stream_state is StreamState.LIVE
        assert provider._event_cursor == latest.cursor
        assert [detail.event.event_kind for detail in details] == [
            record.status for record in records
        ]
        assert all(not detail.event.event_json for detail in details)
        assert [detail.event.invocation_index for detail in details] == [0, 0, 1, 1, 2, 2]
        assert [detail.event.answers for detail in details] == [
            (),
            _answers(),
            (),
            (),
            (),
            (),
        ]
        retained = provider.get_run(run.run_id)
        assert retained is not None and retained.status is RunStatus.FAILED
        assert retained.nodes["classify"].agent_trace_json is None
        assert retained.nodes["classify"].trace is None
    finally:
        provider.close()


def test_missing_classifier_body_is_not_treated_as_known_expiry(transport, monkeypatch):
    operator, service, _ = transport
    run = _seed_run(operator, "missing-body")
    _publish(operator, run.run_id, _invocation(0, "running"))

    def missing_body(body_token):
        raise KeyError(body_token)

    monkeypatch.setattr(operator, "read_detail", missing_body)
    with pytest.raises(grpc.RpcError) as error:
        service.ListRunActivity(
            pb.ListRunActivityRequestV2(run_id=run.run_id, node_id="classify"), timeout=5
        )
    assert error.value.code() is grpc.StatusCode.NOT_FOUND


def test_classifier_summary_bytes_bound_pages_without_truncating_answers(
    transport, monkeypatch
):
    operator, _, _ = transport
    choice = "選" * 1024
    run = _seed_run(operator, "summary-budget", _declaration(choice))
    for index in range(3):
        _publish(operator, run.run_id, _invocation(index, "running", choice=choice))
        _publish(operator, run.run_id, _invocation(index, "success", choice=choice))
    monkeypatch.setattr("runtime.operator.operator.MAX_TRANSPORT_PAGE_BYTES", 6000)

    page = operator.list_classifier_events(run.run_id, "classify")
    retained = []
    while True:
        successful = [item for item in page.events if item.event_kind == "success"]
        assert len(successful) <= 1
        assert all(item.answers == _answers(choice) for item in successful)
        retained.extend(page.events)
        if not page.next_page_token:
            break
        page = operator.list_classifier_events(page_token=page.next_page_token)
    assert [item.event_sequence for item in retained] == [1, 2, 3, 4, 5, 6]
    assert [item.invocation_index for item in retained] == [0, 0, 1, 1, 2, 2]


@pytest.mark.parametrize(
    "summary",
    [
        None,
        pb.ClassifierInvocationSummaryV2(
            answers=[pb.ClassifierAnswerSummaryV2(question_id="match")]
        ),
    ],
)
def test_classifier_descriptor_rejects_missing_typed_summary(summary):
    activity = pb.RunActivityDescriptorV2(kind="classifier_event", classifier_summary=summary)
    with pytest.raises(ValueError):
        classifier_event_descriptor_from_v2(activity)


@pytest.mark.parametrize("limit", ["event", "node", "run"])
def test_input_and_result_share_existing_detail_budgets(transport, monkeypatch, limit):
    operator, service, _ = transport
    run = _seed_run(operator, "bounded-input")
    state = {"ticket": ["x" * 512]}
    running = _invocation(0, "running").model_copy(update={"input": state})
    terminal = _invocation(0, "success").model_copy(update={"input": state})
    running_size = len(running.model_dump_json().encode())
    terminal_size = len(terminal.model_dump_json().encode())
    if limit == "event":
        monkeypatch.setattr(
            "runtime.operator.operator.MAX_CLASSIFIER_EVENT_BYTES", terminal_size - 1
        )
    elif limit == "node":
        monkeypatch.setattr(
            operator, "_max_node_detail_bytes", running_size + terminal_size - 1
        )
    else:
        monkeypatch.setattr(operator, "_max_run_detail_bytes", running_size + terminal_size - 1)

    _publish(operator, run.run_id, running)
    with pytest.raises(_CoordinatorProtocolError):
        _publish(operator, run.run_id, terminal)

    retained = service.ListRunActivity(
        pb.ListRunActivityRequestV2(run_id=run.run_id, node_id="classify")
    )
    assert [_read_invocation(service, item.detail_ref) for item in retained.activities] == [
        running
    ]


@pytest.mark.parametrize(
    "failure",
    [
        "invalid-json",
        "invalid-schema",
        "coerced-required",
        "unknown-node",
        "missing-node",
        "missing-map",
    ],
)
def test_preparation_rejects_malformed_step_interfaces(failure):
    @ava.source
    def load() -> str:
        return "value"

    @ava.workflow
    def flow():
        return load()

    event = {"type": "prepared", **_workflow_metadata(flow())}
    [node_id] = event["node_ids"]
    interfaces = event["step_interface_json"]
    if failure == "invalid-json":
        interfaces[node_id] = "{"
    elif failure == "invalid-schema":
        interface = json.loads(interfaces[node_id])
        interface["step_output"]["json_schema"] = ["private-schema-value"]
        interfaces[node_id] = json.dumps(interface)
    elif failure == "coerced-required":
        interface = json.loads(interfaces[node_id])
        interface["step_inputs"] = [
            {"name": "value", "type_name": "str", "json_schema": None, "required": "false"}
        ]
        interfaces[node_id] = json.dumps(interface)
    elif failure == "unknown-node":
        interfaces["absent"] = interfaces[node_id]
    elif failure == "missing-node":
        del interfaces[node_id]
    else:
        del event["step_interface_json"]

    with pytest.raises(_CoordinatorProtocolError) as error:
        _validate_preparation_event(event)
    assert "private-schema-value" not in str(error.value)


def test_historical_topology_without_step_interfaces_stays_unavailable():
    legacy = pb.WorkflowTopologyV2(
        node_ids=["load"],
        graph={"load": pb.NodeEdgesV2()},
        node_types={"load": "source"},
        display_names={"load": "load"},
    )
    topology = workflow_topology_from_v2(
        pb.WorkflowTopologyV2.FromString(legacy.SerializeToString())
    )
    assert topology.step_interface_json == ()
