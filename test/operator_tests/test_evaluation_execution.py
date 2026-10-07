"""Spawned operator evaluations through the installed SDK and a local HTTP service."""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import queue
import site
import sys
import sysconfig
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import cloudpickle
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

class Handoff(BaseModel):
    summary: str
    payload: dict[str, object]
    producer_pid: int

    def selected_summary(self):
        return self.summary

class Report(BaseModel):
    summary: str
    payload: dict[str, object]
    producer_pid: int
    answers: list[str]

def report_state(ctx):
    return {
        "summary": ctx.output.handoff.selected_summary(),
        "answer": ctx.output.answer,
        "inputs": dict(ctx.inputs),
        "payload": ctx.output.handoff.payload,
        "producer_pid": ctx.output.handoff.producer_pid,
        "selector_pid": os.getpid(),
    }
"""

_WORKFLOW_SOURCE = """
import asyncio
from predict_rlm import RunEvidence, RunTrace
from predict_rlm.trace import IterationStep
import os
import threading
from pathlib import Path
import dspy
import avalanche as ava
{model_import}

class EchoSignature(ava.Signature):
    text: str = ava.InputField()
    payload: dict[str, object] = ava.InputField()
    answer: str = ava.OutputField()
    handoff: Handoff = ava.OutputField()

class Predictor:
    def __init__(self):
        self.world_returning = asyncio.Event()

    async def acall(self, *, text, payload):
        if text == "failed":
            failure = ValueError("agent call failed")
            failure.evidence = RunEvidence(
                run_id="failed-run", complete=True, terminal_outcome="error"
            )
            raise failure
        if text == "hello":
            await self.world_returning.wait()
        handoff = Handoff(
            summary="agent " + text.upper(),
            payload=payload,
            producer_pid=os.getpid(),
        )
        {predictor_body}
        prediction = dspy.Prediction(
            answer=text.upper(),
            handoff=handoff,
            trace=RunTrace(
                status="completed",
                model="test-model",
                iterations=1,
                max_iterations=1,
                duration_ms=10,
                steps=[IterationStep(
                    iteration=1,
                    reasoning="Echo the input",
                    code="SUBMIT(answer=" + repr(text.upper()) + ")",
                    output=text,
                    untruncated_output=text,
                    duration_ms=10,
                )],
            ),
            evidence=RunEvidence(
                run_id=text + "-run", complete=True, terminal_outcome="completed"
            ),
        )
        if text == "world":
            self.world_returning.set()
        return prediction

{extra}
evaluations = ava.Evaluations(
    metrics={{
        "quality": ava.Metric(
            state={selector},
            question={{"type": "noul", "instructions": "Is the agent handoff useful?"}},
        ),
        "trace": ava.Metric(
            state=lambda ctx: ctx.trace,
            question={{"type": "noul", "instructions": "Is this invocation supported?"}},
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
    {before_calls}
    agent_payload = {{"items": list(ticket["items"])}}
    predictions = await asyncio.gather(
        agent(text="hello", payload=agent_payload),
        agent(text="world", payload=agent_payload),
    )
    later = await agent(text="later", payload=agent_payload)
    predictions[1].handoff.summary = "mutated by caller"
    predictions[1].handoff.payload["items"].append("caller output mutation")
    agent_payload["items"].append("caller input mutation")
    predictions[1].trace.steps[0].output = "mutated trace"
    predictions[1].trace.steps[0].untruncated_output = "mutated full trace"
    {body}
    return Report(
        summary="postprocessed " + " ".join(p.answer for p in predictions),
        payload=ticket,
        producer_pid=os.getpid(),
        answers=[p.answer for p in predictions] + [later.answer],
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
    predictor_body: str = "pass",
    before_calls: str = "pass",
) -> Path:
    if package:
        root = root / "evaluation_package"
        root.mkdir()
        (root / "__init__.py").write_text("")
        (root / "models.py").write_text(dedent(_MODEL_SOURCE))
        model_import = "from .models import Handoff, Report, report_state"
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
            predictor_body=predictor_body,
            before_calls=before_calls,
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


def assert_original_requests(service: EvaluationService, *, winner: str = "world") -> None:
    requests = [service.requests.get(timeout=10), service.requests.get(timeout=10)]
    report = next(item for item in requests if "quality" in item["questions"])
    assert report["model"] == "evaluation-workflow-model"
    assert report["state"]["summary"] == "agent " + winner.upper()
    assert report["state"]["answer"] == winner.upper()
    assert report["state"]["inputs"] == {
        "text": winner,
        "payload": {"items": ["original"]},
    }
    assert report["state"]["payload"] == {"items": ["original"]}
    assert report["state"]["selector_pid"] != report["state"]["producer_pid"]
    assert report["state"]["selector_pid"] != os.getpid()
    trace_request = next(item for item in requests if "trace" in item["questions"])
    traces = trace_request["state"]
    assert len(traces) == 1
    assert traces[0]["kind"] == "trace_finished"
    assert traces[0]["trace"]["status"] == "completed"
    assert traces[0]["trace"]["iterations"] == 1
    [iteration] = traces[0]["trace"]["steps"]
    assert iteration["iteration"] == 1
    assert iteration["reasoning"] == "Echo the input"
    assert iteration["code"] == "SUBMIT(answer=" + repr(winner.upper()) + ")"
    assert iteration["output"] == winner
    assert iteration["untruncated_output"] == winner
    assert iteration["duration_ms"] == 10
    assert iteration["error"] is False
    assert iteration["tool_calls"] == []
    assert iteration["predict_calls"] == []
    assert service.requests.empty()


@pytest.mark.parametrize("winner", ["world", "first"])
def test_slow_evaluation_survives_terminal_and_keeps_owned_evidence(
    tmp_path, evaluation_service, winner
):
    workflow = write_workflow(
        tmp_path,
        before_calls=(
            'await agent(text="first", payload=ticket)' if winner == "first" else "pass"
        ),
    )
    evaluation_service.release.clear()
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        assert operator.get_run_result(run.run_id)["summary"] == "mutated downstream"
        assert operator.get_run_result(run.run_id)["payload"] == {
            "items": ["original", "downstream"]
        }
        assert operator.get_run_result(run.run_id)["answers"] == ["HELLO", "WORLD", "LATER"]
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
        assert_original_requests(evaluation_service, winner=winner)
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


@pytest.mark.parametrize("pre_registered", [False, True])
def test_snapshot_owns_source_under_python_prefix_and_restores_registry(
    tmp_path, evaluation_service, monkeypatch, pre_registered
):
    source_root = tmp_path / "app"
    source_root.mkdir()
    source = source_root / "flow.py"
    source.write_text(dedent(_MODEL_SOURCE))
    module_name = "_avalanche_run_prefix_flow"
    spec = importlib.util.spec_from_file_location(module_name, source)
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as snapshot_env:
        snapshot_env.setitem(sys.modules, module_name, module)
        # Multiprocessing can expose one driver as both __main__ and __mp_main__.
        # Registration and cleanup must use the module's actual name just once.
        snapshot_env.setitem(sys.modules, module_name + "_alias", module)
        spec.loader.exec_module(module)
        submission = EvaluationSubmission(
            evaluations=Evaluations(
                metrics={
                    "quality": Metric(
                        state=module.report_state,
                        question={"type": "noul", "instructions": "Is this useful?"},
                    )
                },
                model="evaluation-workflow-model",
            ),
            context=EvalContext(
                inputs={"text": "original"},
                output=SimpleNamespace(
                    handoff=module.Handoff(
                        summary="owned output",
                        payload={"items": ["original"]},
                        producer_pid=os.getpid(),
                    ),
                    answer="READY",
                ),
            ),
            runtime_defaults={},
        )
        snapshot_env.setattr(sys, "prefix", str(tmp_path))
        snapshot_env.chdir(source_root)
        if pre_registered:
            cloudpickle.register_pickle_by_value(module)
        try:
            registered = cloudpickle.list_registry_pickle_by_value()
            request = snapshot_evaluation(submission)
            assert request.error is None, request.error
            assert cloudpickle.list_registry_pickle_by_value() == registered
        finally:
            if pre_registered:
                cloudpickle.unregister_pickle_by_value(module)

    source.unlink()
    assert module_name not in sys.modules
    outcomes = queue.Queue()
    workers = EvaluationWorkers(lambda run_id, evaluation_id, outcome: outcomes.put(outcome))
    try:
        workers.submit("source-under-prefix", request)
        outcome = outcomes.get(timeout=30)
        assert outcome.error is None, outcome.error
        assert outcome.result.classification.nouls["quality"].noul == 0.8
        report = evaluation_service.requests.get(timeout=10)
        assert report["model"] == "evaluation-workflow-model"
        assert report["state"].pop("selector_pid") != os.getpid()
        assert report["state"] == {
            "summary": "owned output",
            "answer": "READY",
            "inputs": {"text": "original"},
            "payload": {"items": ["original"]},
            "producer_pid": os.getpid(),
        }
    finally:
        workers.close()


@pytest.mark.parametrize(
    "library_kind", ["stdlib", "platstdlib", "purelib", "platlib", "site", "user_site"]
)
def test_snapshot_keeps_installed_library_selectors_by_reference(
    tmp_path, monkeypatch, library_kind
):
    paths = {
        kind: str(tmp_path / "installed" / kind)
        for kind in ("stdlib", "platstdlib", "purelib", "platlib")
    }
    monkeypatch.setattr(sysconfig, "get_paths", lambda: paths)
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(tmp_path / "installed" / "site")])
    monkeypatch.setattr(
        site, "getusersitepackages", lambda: str(tmp_path / "installed" / "user_site")
    )
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    library = tmp_path / "installed" / library_kind / "dependency.py"
    library.parent.mkdir(parents=True)
    library.write_text("def select_output(ctx):\n    return ctx.output\n")
    module_name = "_evaluation_installed_dependency"
    spec = importlib.util.spec_from_file_location(module_name, library)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    registered = cloudpickle.list_registry_pickle_by_value()
    request = snapshot_evaluation(
        EvaluationSubmission(
            evaluations=Evaluations(
                metrics={
                    "quality": Metric(
                        state=module.select_output,
                        question={"type": "noul", "instructions": "Is this useful?"},
                    )
                }
            ),
            context=EvalContext(inputs={}, output="completed report"),
            runtime_defaults={},
        )
    )
    assert request.error is None, request.error
    restored = cloudpickle.loads(request.payload)
    assert restored.submission.evaluations.metrics["quality"].state is module.select_output
    assert cloudpickle.list_registry_pickle_by_value() == registered


@pytest.mark.parametrize(
    ("changes", "expected_error"),
    [
        ({"selector": "lambda ctx: ctx.output.missing"}, "State selector"),
        ({"composite": "lambda answers: 2.0"}, "Composite"),
        (
            {"predictor_body": 'handoff.payload["unserializable"] = threading.Lock()'},
            "Evaluation snapshot failed",
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
    workflow = write_workflow(tmp_path, **changes)
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


@pytest.mark.parametrize(
    "before_calls",
    ['raise ValueError("step body failed")', 'await agent(text="failed", payload=ticket)'],
)
def test_failure_before_successful_agent_has_no_evaluation_record(
    tmp_path, evaluation_service, before_calls
):
    workflow = write_workflow(tmp_path, before_calls=before_calls)
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


def test_step_without_agent_call_has_no_evaluation_record(tmp_path, evaluation_service):
    workflow = write_workflow(
        tmp_path,
        before_calls=(
            'return Report(summary="no agent", payload=ticket, '
            "producer_pid=os.getpid(), answers=[])"
        ),
    )
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        assert operator.get_run_result(run.run_id)["summary"] == "mutated downstream"
        assert operator.list_evaluations(run.run_id) == []
        assert evaluation_service.requests.empty()
    finally:
        operator.close()


def test_successful_agent_evaluation_survives_later_step_failure(tmp_path, evaluation_service):
    evaluation_service.release.clear()
    workflow = write_workflow(tmp_path, body='raise ValueError("postprocessing failed")')
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.FAILED
        [pending] = operator.list_evaluations(run.run_id)
        assert pending.status == "pending"
        sequence = operator.current_sequence
        terminal = operator.get_run(run.run_id)
        evaluation_service.release.set()
        record = wait_evaluation(operator, run.run_id)
        assert record.status == "completed", record.error
        assert operator.get_run(run.run_id) == terminal
        assert operator.current_sequence == sequence
        assert_original_requests(evaluation_service)
    finally:
        evaluation_service.release.set()
        operator.close()


def test_capacity_errors_do_not_backpressure_workflow(tmp_path, evaluation_service):
    evaluation_service.release.clear()
    workflow = tmp_path / "many_evaluations.py"
    workflow.write_text(
        dedent("""
        import avalanche as ava
        import dspy
        from predict_rlm import RunEvidence, RunTrace
        from predict_rlm.trace import IterationStep

        class Echo(ava.Signature):
            value: str = ava.InputField()
            answer: str = ava.OutputField()

        evaluations = ava.Evaluations(metrics={
            "quality": ava.Metric(
                state=lambda ctx: ctx.output.answer,
                question={"type": "noul", "instructions": "Is this useful?"},
            )
        })

        @ava.source
        def load():
            return 0

        @ava.agent_step(Echo, evaluations=evaluations)
        async def increment(value: int, *, agent: ava.Agent):
            class Predictor:
                async def acall(self, *, value):
                    return dspy.Prediction(
                        answer=str(value),
                        trace=RunTrace(
                            status="completed",
                            model="test-model",
                            iterations=1,
                            max_iterations=1,
                            duration_ms=1,
                            steps=[IterationStep(
                                iteration=1,
                                reasoning="Echo the input",
                                code="SUBMIT(answer=" + repr(value) + ")",
                                output=value,
                                untruncated_output=value,
                                duration_ms=1,
                            )],
                        ),
                        evidence=RunEvidence(
                            run_id=value + "-run",
                            complete=True,
                            terminal_outcome="completed",
                        ),
                    )

            agent._predictor = Predictor()
            await agent(value=str(value))
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


_INLINE_WORKFLOW_SOURCE = """
import dspy
import predict_rlm
import avalanche as ava

class Draft(ava.Signature):
    \"\"\"Draft a reply.\"\"\"
    text: str = ava.InputField()
    answer: str = ava.OutputField()

class TestPredictor:
    def __init__(self, signature, *, skills=(), tools=(), events=(), **kwargs):
        self.max_iterations = kwargs.get("max_iterations", 2)

    async def acall(self, **inputs):
        return dspy.Prediction(
            answer=inputs["text"].upper(),
            trace=predict_rlm.RunTrace(
                status="completed", model="test-predictor", iterations=0,
                max_iterations=self.max_iterations, duration_ms=1,
            ),
            evidence=predict_rlm.RunEvidence(
                run_id="inline-run", complete=True, terminal_outcome="completed"
            ),
        )

# This replacement exists only in the temporary module's isolated child.
predict_rlm.PredictRLM = TestPredictor

evaluations = ava.Evaluations(metrics={"quality": ava.Metric(
    state=lambda ctx: {"inputs": dict(ctx.inputs), "answer": ctx.output.answer},
    question={"type": "noul", "instructions": "Is the draft useful?"},
)})

@ava.source
def load() -> str:
    return "hello"

@ava.step
def finish(answer: str) -> str:
    return answer + "!"

@ava.workflow(classifier_defaults={"model": "evaluation-workflow-model", "timeout": 120.0})
def flow():
    drafted = ava.agent.step(
        Draft, inputs={"text": load()}, slug="draft", evaluations=evaluations
    )
    return finish(drafted)
"""


def test_inline_agent_evaluation_runs_in_operator(tmp_path, evaluation_service):
    workflow = tmp_path / "inline_evaluation_flow.py"
    workflow.write_text(dedent(_INLINE_WORKFLOW_SOURCE))
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        [info] = operator.list_workflows()
        [draft_node] = info.agent_node_ids
        assert list(info.evaluation_metadata_json) == [draft_node]

        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        assert operator.get_run_result(run.run_id) == "HELLO!"
        record = wait_evaluation(operator, run.run_id)
        assert record.status == "completed", record.error
        assert record.node_id == draft_node
        request = evaluation_service.requests.get(timeout=10)
        assert request["model"] == "evaluation-workflow-model"
        assert request["state"] == {"inputs": {"text": "hello"}, "answer": "HELLO"}
        assert evaluation_service.requests.empty()
    finally:
        operator.close()
