"""Independent evaluation polling over actual gRPC and local JSON HTTP transports."""

from __future__ import annotations

import http.client
import json
import socket

import grpc
import pytest
from pydantic import ValidationError

from avalanche.classifier.models import (
    ChoiceAnswer,
    ClassificationResult,
    ClassificationUsage,
    NoulAnswer,
    ScoreAnswer,
)
from avalanche.evaluations import EvaluationResult
from runtime.operator.evaluation_models import EvaluationRecord
from runtime.operator.models import NodeState, RunState, RunStatus, WorkflowTopology
from runtime.operator.operator import Operator
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc
from runtime.operator.server import serve
from runtime.operator.web import start_browser_server


@pytest.fixture
def transport(tmp_path):
    operator = Operator([], schedule=False, watch=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = serve(operator, port=port, block=False)
    address = f"127.0.0.1:{port}"
    channel = grpc.insecure_channel(address)
    browser = start_browser_server(address, port=0, asset_root=tmp_path)
    try:
        grpc.channel_ready_future(channel).result(timeout=5)
        yield operator, pb_grpc.OperatorServiceV2Stub(channel), browser
    finally:
        browser.close()
        channel.close()
        server.stop(grace=0).wait()
        operator.close()


def _seed_run(operator: Operator, run_id: str) -> RunState:
    node_ids = ("research", "review", "unevaluated")
    run = RunState(
        run_id=run_id,
        flow_name="flow",
        workflow_id="flow.py::flow",
        workflow_display_name="flow",
        status=RunStatus.SUCCESS,
        started_at=100.0,
        ended_at=101.0,
        nodes={node_id: NodeState(node_id, node_id, "step") for node_id in node_ids},
        topology=WorkflowTopology(node_ids=node_ids),
    )
    with operator._lock:
        operator._runs[run_id] = run
    operator._notify_run(run)
    return run


def _store(operator: Operator, record: EvaluationRecord) -> None:
    with operator._lock:
        operator._evaluations.setdefault(record.run_id, {})[record.evaluation_id] = record


def _result() -> EvaluationResult:
    return EvaluationResult(
        classification=ClassificationResult(
            model="jev-latest",
            answers={
                "clarity": ScoreAnswer(
                    type="score",
                    score=1.5,
                    legend={"0": "Confusing", "1": "Mostly clear", "2": "Clear"},
                    probabilities={"0": 0.0, "1": 0.5, "2": 0.5},
                    confidence=0.5,
                ),
                "sources": NoulAnswer(type="noul", noul=0.8),
                "route": ChoiceAnswer(
                    type="choice",
                    choice="accept",
                    probabilities={"accept": 0.9, "reject": 0.1},
                    confidence=0.9,
                ),
            },
            usage=ClassificationUsage(input_tokens=20, output_tokens=10),
        ),
        composites={"quality": 0.765},
    )


def _rest(browser, path: str):
    connection = http.client.HTTPConnection(browser.host, browser.port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        assert response.getheader("Content-Type").split(";")[0] == "application/json"
        assert response.getheader("Cache-Control") == "no-store"
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_pending_then_late_result_survives_terminal_workflow(transport):
    operator, service, browser = transport
    run = _seed_run(operator, "finished")
    request = pb.ListEvaluationsRequestV2(run_id=run.run_id, node_id="research")
    path = "/api/v1/runs/finished/evaluations?node_id=research"
    before = service.GetRunSnapshot(pb.GetRunSnapshotRequestV2(run_id=run.run_id))
    history = operator.update_history_bounds()
    pending = EvaluationRecord(
        evaluation_id="evaluation-1",
        run_id=run.run_id,
        node_id="research",
        status="pending",
        created_at=100.5,
    )
    _store(operator, pending)

    wire_pending = service.ListEvaluations(request, timeout=5).records[0]
    assert wire_pending.evaluation_id == "evaluation-1"
    assert wire_pending.status == "pending"
    assert wire_pending.created_at == 100.5
    for name in ("ended_at", "result_json", "error"):
        assert not wire_pending.HasField(name)
    assert _rest(browser, path) == (
        200,
        {
            "records": [
                {
                    "evaluation_id": "evaluation-1",
                    "run_id": "finished",
                    "node_id": "research",
                    "status": "pending",
                    "created_at": 100.5,
                }
            ]
        },
    )

    result = _result()
    completed = EvaluationRecord(
        evaluation_id=pending.evaluation_id,
        run_id=pending.run_id,
        node_id=pending.node_id,
        status="completed",
        created_at=pending.created_at,
        ended_at=105.0,
        result=result,
    )
    _store(operator, completed)
    wire = service.ListEvaluations(request, timeout=5).records[0]
    assert wire.status == "completed"
    assert wire.ended_at == 105.0
    assert wire.HasField("result_json") and not wire.HasField("error")
    decoded = EvaluationResult.model_validate_json(wire.result_json)
    assert decoded == result
    assert isinstance(decoded.classification.scores["clarity"].score, float)
    assert decoded.classification.scores["clarity"].legend["2"] == "Clear"
    assert decoded.classification.nouls["sources"].noul == 0.8
    assert decoded.classification.choices["route"].probabilities == {
        "accept": 0.9,
        "reject": 0.1,
    }
    assert decoded.composites == {"quality": 0.765}
    status, payload = _rest(browser, path)
    assert status == 200
    assert payload["records"] == [
        {
            "evaluation_id": "evaluation-1",
            "run_id": "finished",
            "node_id": "research",
            "status": "completed",
            "created_at": 100.5,
            "ended_at": 105.0,
            "result_json": wire.result_json,
        }
    ]
    assert isinstance(payload["records"][0]["result_json"], str)
    assert wire_pending.status == "pending"
    assert operator.update_history_bounds() == history
    assert service.GetRunSnapshot(pb.GetRunSnapshotRequestV2(run_id=run.run_id)) == before
    assert run.status is RunStatus.SUCCESS


def test_filters_isolate_runs_nodes_and_failed_evidence(transport):
    operator, service, browser = transport
    for run_id in ("first", "rerun"):
        _seed_run(operator, run_id)
    for evaluation_id, run_id, node_id in (
        ("first-research", "first", "research"),
        ("first-review", "first", "review"),
        ("rerun-research", "rerun", "research"),
    ):
        _store(
            operator,
            EvaluationRecord(
                evaluation_id=evaluation_id,
                run_id=run_id,
                node_id=node_id,
                status="failed",
                created_at=100.5,
                ended_at=105.0,
                error=f"Selector failed for {evaluation_id}",
            ),
        )

    for run_id, node_id, expected in (
        ("first", "", {"first-research", "first-review"}),
        ("first", "research", {"first-research"}),
        ("first", "review", {"first-review"}),
        ("rerun", "research", {"rerun-research"}),
        ("first", "unevaluated", set()),
    ):
        records = service.ListEvaluations(
            pb.ListEvaluationsRequestV2(run_id=run_id, node_id=node_id), timeout=5
        ).records
        assert {record.evaluation_id for record in records} == expected
        status, payload = _rest(browser, f"/api/v1/runs/{run_id}/evaluations?node_id={node_id}")
        assert status == 200
        assert {record["evaluation_id"] for record in payload["records"]} == expected
        for record in records:
            assert record.run_id == run_id
            assert record.status == "failed"
            assert record.error == f"Selector failed for {record.evaluation_id}"
            assert record.HasField("ended_at") and not record.HasField("result_json")
        for record in payload["records"]:
            assert record["run_id"] == run_id
            assert record["status"] == "failed"
            assert record["error"] == f"Selector failed for {record['evaluation_id']}"
            assert record["ended_at"] == 105.0
            assert "result_json" not in record


@pytest.mark.parametrize("run_id,node_id", [("missing", ""), ("known", "missing")])
def test_unknown_evaluation_targets_are_not_found(transport, run_id, node_id):
    operator, service, browser = transport
    _seed_run(operator, "known")
    with pytest.raises(grpc.RpcError) as caught:
        service.ListEvaluations(
            pb.ListEvaluationsRequestV2(run_id=run_id, node_id=node_id), timeout=5
        )
    assert caught.value.code() == grpc.StatusCode.NOT_FOUND
    status, payload = _rest(browser, f"/api/v1/runs/{run_id}/evaluations?node_id={node_id}")
    assert status == 404
    assert payload["error"]["code"] == "NOT_FOUND"


def test_evaluation_request_validation(transport):
    operator, service, browser = transport
    _seed_run(operator, "known")
    with pytest.raises(grpc.RpcError) as caught:
        service.ListEvaluations(pb.ListEvaluationsRequestV2(), timeout=5)
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    for query in ("unexpected=value", "node_id=research&node_id=review"):
        status, payload = _rest(browser, f"/api/v1/runs/known/evaluations?{query}")
        assert status == 400
        assert "error" in payload


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "pending", "ended_at": 101.0},
        {"status": "pending", "result": _result()},
        {"status": "completed", "ended_at": 101.0},
        {"status": "completed", "ended_at": 99.0, "result": _result()},
        {"status": "completed", "ended_at": 101.0, "result": _result(), "error": "bad"},
        {"status": "failed", "ended_at": 101.0, "error": ""},
        {"status": "failed", "ended_at": 101.0, "error": "bad", "result": _result()},
        {"status": "pending", "created_at": float("nan")},
    ],
)
def test_evaluation_record_rejects_contradictory_lifecycle(fields):
    with pytest.raises(ValidationError):
        EvaluationRecord.model_validate(
            {
                "evaluation_id": "evaluation-1",
                "run_id": "run-1",
                "node_id": "research",
                "created_at": 100.0,
                **fields,
            }
        )


def test_evaluation_records_are_frozen_and_results_are_detached(transport):
    operator, _, _ = transport
    _seed_run(operator, "known")
    record = EvaluationRecord(
        evaluation_id="evaluation-1",
        run_id="known",
        node_id="research",
        status="completed",
        created_at=100.0,
        ended_at=101.0,
        result=_result(),
    )
    _store(operator, record)
    snapshot = operator.list_evaluations("known")[0]
    with pytest.raises(ValidationError):
        snapshot.status = "failed"
    snapshot.result.composites["quality"] = 0.0
    snapshot.result.classification.answers.clear()
    retained = operator.list_evaluations("known")[0]
    assert retained.result == _result()
