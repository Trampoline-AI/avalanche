"""Spawned operator evaluations through the installed SDK and a local HTTP service."""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent

import pytest

from avalanche import EvalContext, Evaluations, Metric
from avalanche.evaluation_capture import EvaluationSubmission
from runtime.operator import Operator
from runtime.operator.evaluation_models import EvaluationRecord
from runtime.operator.evaluation_worker import EvaluationWorkers, snapshot_evaluation
from runtime.operator.models import RunState, RunStatus


@dataclass
class EvaluationService:
    origin: str = ""
    api_key: str = "operator-evaluation-http-fixture-key"
    requests: queue.Queue = field(default_factory=queue.Queue)
    release: threading.Event = field(default_factory=threading.Event)
    fail: bool = False


@pytest.fixture
def evaluation_service(monkeypatch):
    """Exercise real SDK serialization/auth; only the HTTP server supplies fixture answers."""
    service = EvaluationService()
    service.release.set()

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
            if not service.release.wait(timeout=120):
                self.send_error(504)
                return
            if service.fail:
                self.send_error(400, "evaluation fixture rejects this request")
                return
            response = json.dumps(
                {
                    "model": "jev-evaluation-http-fixture",
                    "answers": {
                        name: {"type": "noul", "noul": 0.8} for name in payload["questions"]
                    },
                    "usage": {"input_tokens": 13, "output_tokens": 4},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            try:
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    service.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TYPESAFE_API_KEY", service.api_key)
    monkeypatch.setenv("TYPESAFE_BASE_URL", service.origin)
    try:
        yield service
    finally:
        service.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


_MODEL_SOURCE = """
import os
from pydantic import BaseModel

class Report(BaseModel):
    summary: str
    payload: dict[str, object]
    producer_pid: int

    def selected_summary(self):
        return self.summary

def report_state(ctx):
    return {
        "summary": ctx.output.selected_summary(),
        "inputs": dict(ctx.inputs),
        "payload": ctx.output.payload,
        "producer_pid": ctx.output.producer_pid,
        "selector_pid": os.getpid(),
    }
"""

_WORKFLOW_SOURCE = """
import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
import avalanche as ava
{model_import}

class EchoSignature(ava.Signature):
    text: str = ava.InputField()
    answer: str = ava.OutputField()

class Trace:
    def __init__(self, text):
        self.text = text

    def to_exportable_json(self):
        return json.dumps({{"status": "completed", "steps": [{{"text": self.text}}]}})

class Predictor:
    async def acall(self, *, text):
        await asyncio.sleep(0)
        if text == "missing":
            return SimpleNamespace(answer=text)
        return SimpleNamespace(answer=text.upper(), trace=Trace(text))

{extra}
evaluations = ava.Evaluations(
    metrics={{
        "quality": ava.Metric(
            state={selector},
            question={{"type": "noul", "instructions": "Is the report useful?"}},
        ),
        "trace": ava.Metric(
            state=lambda ctx: ctx.trace,
            question={{"type": "noul", "instructions": "Are all invocations supported?"}},
        ),
    }},
    composites={{"quality": {composite}}},
)

@ava.source
def load():
    return {{"items": [Path(__file__).with_suffix(".txt").read_text()]}}

@ava.agent_step(EchoSignature, evaluations=evaluations)
async def research(ticket: dict, repeat: int = 2, *, agent: ava.Agent,
                   context: ava.RunContext, logger=ava.Logger()) -> Report:
    agent._predictor = Predictor()
    predictions = await asyncio.gather(agent(text="hello"), agent(text="world"))
    await agent(text="missing")
    {body}
    return Report(
        summary=" ".join(p.answer for p in predictions),
        payload=ticket,
        producer_pid=os.getpid(),
    )

@ava.step
def finish(report: Report):
    report.summary = "mutated downstream"
    report.payload["items"].append("downstream")
    return report.model_dump()

@ava.workflow(classifier_defaults={{"model": "evaluation-workflow-model", "timeout": 120.0}})
def flow():
    return finish(research(load()))
"""


def write_workflow(
    root: Path,
    *,
    package: bool = False,
    selector: str = "report_state",
    composite: str = 'lambda answers: answers.nouls["quality"].noul',
    extra: str = "",
    body: str = "pass",
) -> Path:
    if package:
        root = root / "evaluation_package"
        root.mkdir()
        (root / "__init__.py").write_text("")
        (root / "models.py").write_text(dedent(_MODEL_SOURCE))
        model_import = "from .models import Report, report_state"
    else:
        model_import = dedent(_MODEL_SOURCE)
    workflow = root / "evaluation_flow.py"
    workflow.write_text(
        dedent(_WORKFLOW_SOURCE).format(
            model_import=model_import,
            selector=selector,
            composite=composite,
            extra=extra,
            body=body,
        )
    )
    workflow.with_suffix(".txt").write_text("original")
    return workflow


def wait_terminal(operator: Operator, run_id: str, timeout: float = 30) -> RunState:
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
    raise AssertionError(f"Workflow {run_id} did not terminate")


def wait_evaluation(operator: Operator, run_id: str, timeout: float = 30) -> EvaluationRecord:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = operator.list_evaluations(run_id)
        if records and records[0].status != "pending":
            [record] = records
            return record
        time.sleep(0.03)
    raise AssertionError(f"Evaluation for {run_id} did not finish")


def evaluation_node(run: RunState) -> str:
    return next(node_id for node_id, node in run.nodes.items() if node.name == "research")


def assert_original_requests(service: EvaluationService) -> None:
    requests = [service.requests.get(timeout=10), service.requests.get(timeout=10)]
    report = next(item for item in requests if "quality" in item["questions"])
    assert report["model"] == "evaluation-workflow-model"
    assert report["state"]["summary"] == "HELLO WORLD"
    assert report["state"]["inputs"] == {"ticket": {"items": ["original"]}, "repeat": 2}
    assert report["state"]["payload"] == {"items": ["original"]}
    assert report["state"]["selector_pid"] != report["state"]["producer_pid"]
    assert report["state"]["selector_pid"] != os.getpid()
    trace_request = next(item for item in requests if "trace" in item["questions"])
    traces = trace_request["state"]
    assert len({item["invocation_id"] for item in traces}) == 3
    assert [item["trace"]["steps"] for item in traces if item["kind"] == "trace_finished"] == [
        [{"text": "hello"}],
        [{"text": "world"}],
    ]
    assert [item["error"] for item in traces if item["kind"] == "trace_unavailable"] == [
        "Agent trace unavailable"
    ]


def test_slow_evaluation_survives_terminal_and_keeps_owned_evidence(
    tmp_path, evaluation_service
):
    workflow = write_workflow(tmp_path)
    evaluation_service.release.clear()
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        assert operator.get_run_result(run.run_id)["summary"] == "mutated downstream"
        assert operator.get_run_result(run.run_id)["payload"] == {
            "items": ["original", "downstream"]
        }
        [pending] = operator.list_evaluations(run.run_id, evaluation_node(run))
        assert pending.status == "pending"
        assert pending.result is None and pending.error is None
        sequence = operator.current_sequence
        terminal = operator.get_run(run.run_id)
        evaluation_service.release.set()
        record = wait_evaluation(operator, run.run_id)
        assert record.status == "completed", record.error
        assert record.result.composites == {"quality": 0.8}
        assert record.result.classification.nouls["quality"].noul == 0.8
        assert operator.get_run(run.run_id) == terminal
        assert operator.current_sequence == sequence
        assert pending.status == "pending"
        record.result.composites["quality"] = 0
        assert operator.list_evaluations(run.run_id)[0].result.composites == {"quality": 0.8}
        assert_original_requests(evaluation_service)
    finally:
        evaluation_service.release.set()
        operator.close()


@pytest.mark.parametrize("package", [False, True])
def test_owned_source_definitions_and_separate_reruns(tmp_path, evaluation_service, package):
    workflow = write_workflow(tmp_path, package=package)
    evaluation_service.release.clear()
    module_names = set(sys.modules)
    cwd = Path.cwd()
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        runs = []
        # Occupy both evaluators so the third snapshot cannot deserialize until
        # after workflow source is removed. No private worker controls are used.
        for index in range(3):
            workflow.with_suffix(".txt").write_text(f"run-{index}")
            run = wait_terminal(operator, operator.start_run("flow"))
            assert run.status == RunStatus.SUCCESS
            runs.append(run)
            if index < 2:
                request = evaluation_service.requests.get(timeout=20)
                assert request["state"]["payload"] == {"items": [f"run-{index}"]}
        for path in workflow.parent.glob("*.py"):
            path.unlink()
        evaluation_service.release.set()
        records = [wait_evaluation(operator, run.run_id) for run in runs]
        assert [record.status for record in records] == ["completed"] * 3
        assert len({record.evaluation_id for record in records}) == 3
        assert {record.run_id for record in records} == {run.run_id for run in runs}
        remaining_requests = [evaluation_service.requests.get(timeout=10) for _ in range(4)]
        [third] = [item for item in remaining_requests if "quality" in item["questions"]]
        assert third["state"]["payload"] == {"items": ["run-2"]}
        assert Path.cwd() == cwd
        assert not any(
            name.startswith(("evaluation_package", "_avalanche_run_evaluation_flow"))
            for name in set(sys.modules) - module_names
        )
    finally:
        evaluation_service.release.set()
        operator.close()


@pytest.mark.parametrize(
    ("changes", "expected_error"),
    [
        ({"selector": "lambda ctx: ctx.output.missing"}, "State selector"),
        ({"composite": "lambda answers: 2.0"}, "Composite"),
        ({"body": 'ticket["unserializable"] = threading.Lock()'}, "Evaluation snapshot failed"),
        (
            {
                "body": "from avalanche._agent_evidence import emit_agent_evidence; "
                "emit_agent_evidence({})"
            },
            "KeyError",
        ),
        (
            {"extra": "def fail_worker(ctx):\n    os._exit(19)", "selector": "fail_worker"},
            "Evaluation worker failed",
        ),
    ],
)
def test_evaluation_errors_do_not_fail_workflows(
    tmp_path, evaluation_service, changes, expected_error
):
    # Remove an intentionally unserializable field downstream so workflow result
    # transport remains valid; the evaluation must still report the snapshot error.
    workflow = write_workflow(tmp_path, **changes)
    if "body" in changes:
        source = workflow.read_text().replace(
            'report.summary = "mutated downstream"',
            'report.payload.pop("unserializable", None)\n'
            '    report.summary = "mutated downstream"',
        )
        workflow.write_text(source)
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        record = wait_evaluation(operator, run.run_id)
        assert record.status == "failed"
        assert record.result is None
        assert expected_error in record.error
        assert operator.get_run_result(run.run_id)["summary"] == "mutated downstream"
    finally:
        operator.close()


def test_evaluation_result_survives_process_exit_between_poll_and_liveness():
    class StalePollReceiver:
        def __init__(self, receiver, context):
            self.receiver = receiver
            self.context = context
            self.observed = False

        def poll(self, timeout=0):
            if not self.observed:
                # Model a poll that saw no data just before the child sent and exited.
                # Wait for both facts so the liveness observation cannot win by luck.
                assert self.receiver.poll(10)
                self.context.process.join(timeout=10)
                assert not self.context.process.is_alive()
                self.observed = True
                return False
            return self.receiver.poll(timeout)

        def recv_bytes(self, maximum):
            return self.receiver.recv_bytes(maximum)

        def close(self):
            self.receiver.close()

    class ExitDeliveryContext:
        def __init__(self):
            self.real = multiprocessing.get_context("spawn")

        def Pipe(self, **kwargs):  # noqa: N802 - multiprocessing context interface
            receiver, sender = self.real.Pipe(**kwargs)
            return StalePollReceiver(receiver, self), sender

        def Process(self, **kwargs):  # noqa: N802 - multiprocessing context interface
            self.process = self.real.Process(**kwargs)
            return self.process

    # A genuine selector failure exercises the evaluator without any network request.
    request = snapshot_evaluation(
        EvaluationSubmission(
            evaluations=Evaluations(
                metrics={
                    "clarity": Metric(
                        state=lambda ctx: ctx.output.missing_field,
                        question={"type": "noul", "instructions": "Is this clear?"},
                    )
                }
            ),
            context=EvalContext(inputs={}, output="completed report"),
            runtime_defaults={},
        )
    )
    outcomes = queue.Queue()
    workers = EvaluationWorkers(lambda run_id, evaluation_id, outcome: outcomes.put(outcome))
    workers._mp = ExitDeliveryContext()
    try:
        workers.submit("exit-delivery-race", request)
        outcome = outcomes.get(timeout=30)
        assert outcome.result is None
        assert outcome.error is not None
        assert "AttributeError" in outcome.error
        assert "clarity" in outcome.error
    finally:
        workers.close()


def test_service_failure_is_an_evaluation_error(tmp_path, evaluation_service):
    evaluation_service.fail = True
    workflow = write_workflow(tmp_path)
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        record = wait_evaluation(operator, run.run_id)
        assert record.status == "failed"
        assert "Jev evaluation" in record.error
        assert record.result is None
    finally:
        operator.close()


def test_project_dotenv_credentials_are_not_loaded_into_operator(
    tmp_path, evaluation_service, monkeypatch
):
    # LiteLLM's development import hook otherwise loads the checkout's .env before
    # the coordinator switches to this test's separate project directory.
    monkeypatch.setenv("LITELLM_MODE", "PRODUCTION")
    monkeypatch.delenv("TYPESAFE_API_KEY")
    (tmp_path / ".env").write_text(
        f"TYPESAFE_API_KEY={evaluation_service.api_key}\n"
        "TYPESAFE_BASE_URL=http://unreachable-dotenv-value.invalid\n"
    )
    workflow = write_workflow(tmp_path)
    cwd = Path.cwd()
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        record = wait_evaluation(operator, run.run_id)
        assert record.status == "completed", record.error
        assert "TYPESAFE_API_KEY" not in os.environ
        assert os.environ["TYPESAFE_BASE_URL"] == evaluation_service.origin
        assert Path.cwd() == cwd
    finally:
        operator.close()


def test_failed_step_has_no_evaluation_record(tmp_path, evaluation_service):
    workflow = write_workflow(tmp_path, body='raise ValueError("step body failed")')
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.FAILED
        assert operator.list_evaluations(run.run_id) == []
        assert evaluation_service.requests.empty()
        with pytest.raises(KeyError):
            operator.list_evaluations("unknown-run")
        with pytest.raises(KeyError):
            operator.list_evaluations(run.run_id, "unknown-node")
    finally:
        operator.close()


def test_capacity_errors_do_not_backpressure_workflow(tmp_path, evaluation_service):
    evaluation_service.release.clear()
    workflow = tmp_path / "many_evaluations.py"
    workflow.write_text(
        dedent("""
        import avalanche as ava

        class Echo(ava.Signature):
            value: str = ava.InputField()
            answer: str = ava.OutputField()

        evaluations = ava.Evaluations(metrics={
            "quality": ava.Metric(
                state=lambda ctx: str(ctx.output),
                question={"type": "noul", "instructions": "Is this useful?"},
            )
        })

        @ava.source
        def load():
            return 0

        @ava.agent_step(Echo, evaluations=evaluations)
        def increment(value: int, *, agent: ava.Agent):
            return value + 1

        @ava.workflow(classifier_defaults={"timeout": 60.0})
        def flow():
            value = load()
            for _ in range(40):
                value = increment(value)
            return value
    """)
    )
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        assert operator.get_run_result(run.run_id) == 40
        records = operator.list_evaluations(run.run_id)
        assert len(records) == 40
        assert len({record.node_id for record in records}) == 40
        failed = [record for record in records if record.status == "failed"]
        assert failed
        assert all("Evaluation queue is full" in record.error for record in failed)
        assert any(record.status == "pending" for record in records)
        operator.close()
        assert all(
            record.status == "failed" for record in operator.list_evaluations(run.run_id)
        )
    finally:
        evaluation_service.release.set()
        operator.close()


def test_close_interrupts_evaluations_without_leaking_processes(tmp_path, evaluation_service):
    workflow = write_workflow(tmp_path)
    evaluation_service.release.clear()
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        evaluation_service.requests.get(timeout=20)
        before_close = operator.current_sequence
        started = time.monotonic()
        operator.close()
        assert time.monotonic() - started < 10
        record = wait_evaluation(operator, run.run_id)
        assert record.status == "failed"
        assert "closed" in record.error
        assert operator.current_sequence == before_close
        assert operator.get_run(run.run_id).status == RunStatus.SUCCESS
        assert not any(
            process.name == "avalanche-evaluator"
            for process in multiprocessing.active_children()
        )
        assert not any(
            thread.name.startswith("avalanche-evaluation-") for thread in threading.enumerate()
        )
    finally:
        evaluation_service.release.set()
        operator.close()


@pytest.mark.ray
def test_ray_evaluation_survives_ray_and_coordinator_shutdown(tmp_path, evaluation_service):
    pytest.importorskip("ray")
    workflow = write_workflow(tmp_path)
    evaluation_service.release.clear()
    operator = Operator(
        [str(workflow)],
        executor_backend="ray",
        ray_init_kwargs={"address": "local", "num_cpus": 2, "include_dashboard": False},
        ray_runtime_env={
            "env_vars": {
                "TYPESAFE_BASE_URL": evaluation_service.origin,
                "TYPESAFE_API_KEY": evaluation_service.api_key,
            }
        },
        prepare_timeout=30,
        watch=False,
        schedule=False,
    )
    try:
        run = wait_terminal(operator, operator.start_run("flow"), timeout=120)
        assert run.status == RunStatus.SUCCESS
        assert operator.list_evaluations(run.run_id)[0].status == "pending"
        sequence = operator.current_sequence
        evaluation_service.release.set()
        record = wait_evaluation(operator, run.run_id)
        assert record.status == "completed", record.error
        assert operator.current_sequence == sequence
        assert operator.get_run(run.run_id).status == RunStatus.SUCCESS
        assert_original_requests(evaluation_service)
    finally:
        evaluation_service.release.set()
        operator.close()
