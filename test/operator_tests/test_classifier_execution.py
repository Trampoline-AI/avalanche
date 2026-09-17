"""Classifier evidence retained across real SDK calls and spawned operator workers."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent, indent

import pytest

from avalanche.classifier.models import ClassificationResult, ClassifierInvocation
from runtime.operator import Operator
from runtime.operator.models import NodeStatus, RunState, RunStatus
from runtime.operator.operator import RunResultUnavailableError

QUESTIONS = {
    "urgent": {
        "type": "noul",
        "instructions": {"ask": ["Does this require action today?", {"business_hours": True}]},
    },
    "severity": {
        "type": "score",
        "instructions": ["Assess severity", {"consider": "customer impact"}],
        "criteria": ["minor", {"impact": ["service unavailable"]}],
    },
    "department": {
        "type": "choice",
        "instructions": "Route the ticket",
        "criteria": {"billing": {"examples": ["duplicate charge"]}, "technical": None},
    },
}


@dataclass
class TypeSafeService:
    origin: str = ""
    api_key: str = "operator-classifier-test-only-key"
    requests: queue.Queue = field(default_factory=queue.Queue)
    release: threading.Event = field(default_factory=threading.Event)
    concurrent: threading.Barrier = field(default_factory=lambda: threading.Barrier(2))


@pytest.fixture
def typesafe_service(monkeypatch):
    """Use the installed SDK's origin override, never a replacement SDK client."""
    service = TypeSafeService()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - HTTP server interface
            if self.path != "/v1/systemone":
                self.send_error(404)
                return
            if self.headers.get("Authorization") != f"Bearer {service.api_key}":
                self.send_error(401)
                return
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            service.requests.put(payload)
            state = payload["state"]
            if isinstance(state, dict) and state.get("block"):
                if not service.release.wait(timeout=30):
                    self.send_error(504)
                    return
            if isinstance(state, dict) and state.get("concurrent"):
                try:
                    service.concurrent.wait(timeout=15)
                except threading.BrokenBarrierError:
                    self.send_error(504)
                    return
            probability = (
                state.get("urgent_probability", 0.91) if isinstance(state, dict) else 0.91
            )
            levels = payload["questions"]["severity"]["criteria"]
            response = json.dumps(
                {
                    "model": "jev-test-resolved",
                    "answers": {
                        "department": {
                            "type": "choice",
                            "choice": "billing",
                            "probabilities": {"billing": 0.8, "technical": 0.2},
                            "confidence": 0.6,
                        },
                        "urgent": {"type": "noul", "noul": probability},
                        "severity": {
                            "type": "score",
                            "score": 0.75,
                            "legend": {str(index): level for index, level in enumerate(levels)},
                            "probabilities": {"0": 0.25, "1": 0.75},
                            "confidence": 0.5,
                        },
                    },
                    "usage": {"input_tokens": 37, "output_tokens": 11},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            try:
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                # Cancellation intentionally closes an in-flight SDK connection.
                pass

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    service.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TYPESAFE_BASE_URL", service.origin)
    monkeypatch.setenv("TYPESAFE_API_KEY", service.api_key)
    try:
        yield service
    finally:
        service.release.set()
        service.concurrent.abort()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def write_workflow(root: Path, body: str, *, questions=None) -> Path:
    workflow = root / "classifier_flow.py"
    workflow.write_text(
        "import asyncio\n"
        "import os\n"
        "import avalanche as ava\n"
        "@ava.source\n"
        "def load():\n"
        "    return {'private_ticket': 'operator-private-state'}\n"
        f"@ava.classifier_step(questions={QUESTIONS if questions is None else questions!r})\n"
        "async def classify(ticket, *, classifier: ava.Classifier):\n"
        + indent(dedent(body).strip(), "    ")
        + "\n@ava.workflow(classifier_defaults={"
        "'model': 'operator-request-model', 'timeout': 10.0})\n"
        "def flow():\n"
        "    return classify(load())\n"
    )
    return workflow


def wait_terminal(operator: Operator, run_id: str, timeout: float = 20.0) -> RunState:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = operator.get_run(run_id)
        if run is not None and run.status in {
            RunStatus.SUCCESS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return run
        time.sleep(0.03)
    raise AssertionError(f"run {run_id} did not finish")


def classifier_node(run: RunState) -> str:
    [node_id] = dict(run.topology.classifier_metadata_json)
    return node_id


def read_invocations(operator: Operator, run: RunState) -> list[ClassifierInvocation]:
    """Materialize only the public descriptor/detail API, not operator internals."""
    node_id = classifier_node(run)
    page = operator.list_classifier_events(run.run_id, node_id, page_size=2)
    records = []
    while True:
        records.extend(
            ClassifierInvocation.model_validate_json(operator.read_detail(event.body_token))
            for event in page.events
        )
        if not page.next_page_token:
            return records
        page = operator.list_classifier_events(page_token=page.next_page_token, page_size=2)


def assert_mixed_answers(result: ClassificationResult | None) -> None:
    assert result is not None
    assert result.model == "jev-test-resolved"
    assert result.choices["department"].choice == "billing"
    assert result.choices["department"].probabilities == {"billing": 0.8, "technical": 0.2}
    assert result.choices["department"].confidence == 0.6
    assert result.nouls["urgent"].noul == 0.91
    assert result.scores["severity"].score == 0.75
    assert result.scores["severity"].legend == {
        "0": "minor",
        "1": {"impact": ["service unavailable"]},
    }
    assert result.scores["severity"].probabilities == {"0": 0.25, "1": 0.75}
    assert result.scores["severity"].confidence == 0.5
    assert (result.usage.input_tokens, result.usage.output_tokens) == (37, 11)


def test_spawned_local_workflow_returns_all_answer_types(tmp_path, typesafe_service):
    workflow = write_workflow(tmp_path, "return await classifier(state=ticket)")
    operator = Operator([str(workflow)], executor_backend="local", watch=False, schedule=False)
    try:
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        assert_mixed_answers(
            ClassificationResult.model_validate(operator.get_run_result(run.run_id))
        )
        records = read_invocations(operator, run)
        assert [record.status for record in records] == ["running", "success"]
        assert records[0].invocation_id == records[1].invocation_id
        assert_mixed_answers(records[1].result)
        sent_input = typesafe_service.requests.get(timeout=5)["state"]
        assert all(record.input == sent_input for record in records)
        assert typesafe_service.api_key not in "".join(
            record.model_dump_json() for record in records
        )
        assert operator.list_agent_events(run.run_id, classifier_node(run)).events == ()
    finally:
        operator.close()


@pytest.mark.parametrize("fail_after_classification", [False, True])
def test_postprocessing_does_not_replace_successful_classifier_answers(
    tmp_path, typesafe_service, fail_after_classification
):
    ending = (
        "raise RuntimeError('postprocessing rejected the classification')"
        if fail_after_classification
        else "return {'queue': result.choices['department'].choice, 'notify': True}"
    )
    workflow = write_workflow(
        tmp_path,
        "result = await classifier(state=ticket)\n"
        "ticket['private_ticket'] = 'mutated after request'\n"
        "result.choices['department'].probabilities['billing'] = 0.0\n" + ending,
    )
    operator = Operator([str(workflow)], executor_backend="local", watch=False, schedule=False)
    try:
        run = wait_terminal(operator, operator.start_run("flow"))
        if fail_after_classification:
            assert run.status == RunStatus.FAILED
            assert run.nodes[classifier_node(run)].status == NodeStatus.FAILED
            with pytest.raises(RunResultUnavailableError):
                operator.get_run_result(run.run_id)
        else:
            assert run.status == RunStatus.SUCCESS
            assert operator.get_run_result(run.run_id) == {"queue": "billing", "notify": True}
        records = read_invocations(operator, run)
        assert [record.status for record in records] == ["running", "success"]
        assert_mixed_answers(records[-1].result)
        sent_input = typesafe_service.requests.get(timeout=5)["state"]
        assert all(record.input == sent_input for record in records)
    finally:
        operator.close()


MULTIPLE_CALL_BODY = """
states = [
    {**ticket, 'urgent_probability': probability, 'concurrent': True,
     'history': [{'phase': 'submitted'}]}
    for probability in (0.2, 0.8)
]
results = await asyncio.gather(
    *(classifier(state=state) for state in states)
)
for state in states:
    state['urgent_probability'] = 0.5
    state['history'][0]['phase'] = 'mutated after response'
return {
    'probabilities': [result.nouls['urgent'].noul for result in results],
    'pid': os.getpid(),
}
"""


def assert_distinct_calls(operator: Operator, run: RunState, requests: queue.Queue) -> set[str]:
    grouped: dict[str, list[ClassifierInvocation]] = defaultdict(list)
    for record in read_invocations(operator, run):
        grouped[record.invocation_id].append(record)
    assert len(grouped) == 2
    probabilities = {}
    sent_inputs = {}
    for _ in grouped:
        state = requests.get(timeout=5)["state"]
        sent_inputs[state["urgent_probability"]] = state
    for records in grouped.values():
        assert [record.status for record in records] == ["running", "success"]
        assert records[0].invocation_index == records[1].invocation_index
        assert records[0].result is None
        result = records[1].result
        assert result is not None
        probabilities[records[0].invocation_index] = result.nouls["urgent"].noul
        assert records[0].input == records[1].input == sent_inputs[result.nouls["urgent"].noul]
    assert probabilities == {0: 0.2, 1: 0.8}
    return set(grouped)


def test_concurrent_calls_and_later_runs_retain_distinct_invocations(
    tmp_path, typesafe_service
):
    workflow = write_workflow(tmp_path, MULTIPLE_CALL_BODY)
    operator = Operator([str(workflow)], executor_backend="local", watch=False, schedule=False)
    try:
        identities = set()
        for _ in range(2):
            run = wait_terminal(operator, operator.start_run("flow"))
            assert run.status == RunStatus.SUCCESS
            output = operator.get_run_result(run.run_id)
            assert output["probabilities"] == [0.2, 0.8]
            assert output["pid"] != os.getpid()
            current = assert_distinct_calls(operator, run, typesafe_service.requests)
            assert identities.isdisjoint(current)
            identities.update(current)
    finally:
        operator.close()


def test_cancelled_inflight_request_retains_declaration_without_fabricated_answers(
    tmp_path, typesafe_service
):
    workflow = write_workflow(
        tmp_path, "return await classifier(state={**ticket, 'block': True})"
    )
    operator = Operator(
        [str(workflow)], executor_backend="local", watch=False, schedule=False, cancel_grace=0.2
    )
    try:
        run_id = operator.start_run("flow")
        sent_input = typesafe_service.requests.get(timeout=15)["state"]
        deadline = time.monotonic() + 5
        while True:
            run = operator.get_run(run_id)
            assert run is not None
            records = read_invocations(operator, run)
            if records:
                break
            assert time.monotonic() < deadline
            time.sleep(0.03)
        assert [record.status for record in records] == ["running"]
        assert records[0].input == sent_input
        operator.cancel_run(run_id)
        cancelled = wait_terminal(operator, run_id)
        assert cancelled.status == RunStatus.CANCELLED
        assert cancelled.nodes[classifier_node(cancelled)].status != NodeStatus.RUNNING
        records = read_invocations(operator, cancelled)
        assert records[0].status == "running"
        assert all(record.result is None for record in records)
        assert all(record.input == sent_input for record in records)
        assert all(record.status in {"running", "cancelled"} for record in records)
        assert list(records[0].declaration.questions) == ["urgent", "severity", "department"]
        with pytest.raises(RunResultUnavailableError):
            operator.get_run_result(run_id)
    finally:
        operator.close()
        typesafe_service.release.set()


@pytest.mark.ray
def test_ray_operator_forwards_classifier_invocations_through_worker_queue(
    tmp_path, typesafe_service
):
    pytest.importorskip("ray")
    workflow = write_workflow(tmp_path, MULTIPLE_CALL_BODY)
    operator = Operator(
        [str(workflow)],
        executor_backend="ray",
        ray_init_kwargs={"address": "local", "num_cpus": 2, "include_dashboard": False},
        ray_runtime_env={
            "env_vars": {
                "TYPESAFE_BASE_URL": typesafe_service.origin,
                "TYPESAFE_API_KEY": typesafe_service.api_key,
            }
        },
        prepare_timeout=30.0,
        watch=False,
        schedule=False,
    )
    try:
        run = wait_terminal(operator, operator.start_run("flow"), timeout=90)
        assert run.status == RunStatus.SUCCESS
        output = operator.get_run_result(run.run_id)
        assert output["probabilities"] == [0.2, 0.8]
        assert output["pid"] != os.getpid()
        assert_distinct_calls(operator, run, typesafe_service.requests)
        assert operator.list_agent_events(run.run_id, classifier_node(run)).events == ()
    finally:
        operator.close()
