"""Evaluation capture preserves step execution and existing evidence observers."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import warnings
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from ray import cloudpickle

import avalanche as ava
from avalanche._agent_evidence import capture_agent_evidence, emit_agent_evidence
from avalanche.agent import AgentStepExecutionError
from avalanche.evaluation_capture import capture_evaluations

agent_module = importlib.import_module("avalanche.agent.agent_step")
capture_module = importlib.import_module("avalanche.evaluation_capture")


class EchoSignature(ava.Signature):
    text: str = ava.InputField()
    answer: str = ava.OutputField()


class Report(BaseModel):
    summary: str


class Trace:
    def __init__(self, text, status="completed"):
        self.text = text
        self.status = status

    def to_exportable_json(self):
        return json.dumps({"status": self.status, "steps": [{"text": self.text}]})


def forbidden_evaluation(*args, **kwargs):
    raise AssertionError("capture must not execute evaluation code")


@pytest.fixture
def evaluations(monkeypatch):
    monkeypatch.setattr(ava.Evaluations, "evaluate", forbidden_evaluation)
    return ava.Evaluations(
        metrics={
            "quality": ava.Metric(
                state=forbidden_evaluation,
                question={"type": "noul", "instructions": "Is the answer useful?"},
            )
        }
    )


def test_declaration_and_discovery_do_not_execute_evaluations(evaluations, monkeypatch):
    monkeypatch.setattr(agent_module, "_build_predictor", forbidden_evaluation)

    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(text: str, *, agent: ava.Agent) -> str:
        raise AssertionError("discovery must not execute the step body")

    @ava.workflow
    def flow():
        return echo("hello")

    assert "evaluations" in inspect.signature(ava.agent_step).parameters
    assert echo.fn.__agent_step__.evaluations is evaluations
    metadata = echo.fn.__agent_step__.declaration_metadata()
    assert [field["name"] for field in metadata["signature"]["inputs"]] == ["text"]
    assert list(inspect.signature(echo.fn).parameters) == ["text"]
    assert flow().name == "flow"
    assert ava.EvalContext(inputs={"text": "hello"}, output="HELLO").output == "HELLO"


@pytest.mark.asyncio
async def test_embedded_execution_reports_not_evaluated(evaluations, caplog):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    def echo(text: str, *, agent: ava.Agent) -> str:
        return text.upper()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = await echo.fn("hello")

    assert result == "HELLO"
    assert "not evaluated" in caplog.text


@pytest.mark.asyncio
async def test_final_output_and_bound_defaults_exclude_injected_values(
    evaluations,
    monkeypatch,
):
    class Predictor:
        async def acall(self, *, text):
            return SimpleNamespace(answer=text.upper(), trace=Trace(text))

    def build_predictor(*args, **kwargs):
        assert "evaluations" not in kwargs
        return Predictor()

    monkeypatch.setattr(agent_module, "_build_predictor", build_predictor)

    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(
        text: str,
        /,
        repeat: int = 2,
        *,
        agent: ava.Agent,
        context: ava.RunContext,
        logger=ava.Logger(),
    ) -> Report:
        prediction = await agent(text=text)
        return Report(summary=" ".join([prediction.answer] * repeat))

    submissions = []
    with capture_evaluations(submissions.append) as errors:
        result = await echo.fn("hello", context=object())

    assert result == Report(summary="HELLO HELLO")
    assert errors == []
    assert len(submissions) == 1
    submission = submissions[0]
    assert submission.error is None
    assert submission.context.inputs == {"text": "hello", "repeat": 2}
    assert submission.context.output is result
    assert submission.context.trace[0]["trace"]["steps"] == [{"text": "hello"}]


@pytest.mark.asyncio
async def test_all_invocations_and_unavailable_traces_preserve_prior_observer(
    evaluations,
    monkeypatch,
):
    class Predictor:
        async def acall(self, *, text):
            emit_agent_evidence(
                {
                    "kind": "evidence",
                    "invocation_id": agent_module._current_invocation_state().invocation_id,
                    "sequence": 1,
                    "event_kind": "run_started",
                    "timestamp_ns": 1,
                    "data": {"text": text},
                }
            )
            await asyncio.sleep(0)
            if text == "missing":
                return SimpleNamespace(answer=text)
            if text == "failed":
                failure = ValueError("provider failed")
                failure.trace = Trace(text, status="failed")
                raise failure
            return SimpleNamespace(answer=text, trace=Trace(text))

    monkeypatch.setattr(agent_module, "_build_predictor", lambda *a, **kw: Predictor())

    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(*, agent: ava.Agent) -> str:
        await asyncio.gather(*(agent(text=text) for text in ("first", "second", "missing")))
        try:
            await agent(text="failed")
        except AgentStepExecutionError:
            pass
        return "final answer"

    observed = []
    submissions = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with capture_evaluations(submissions.append):
            assert await echo.fn() == "final answer"
        emit_agent_evidence(
            {"kind": "trace_unavailable", "invocation_id": "outside", "error": "outside"}
        )

    assert observed[-1]["invocation_id"] == "outside"
    terminal = [event for event in observed[:-1] if event["kind"] != "evidence"]
    trace = submissions[0].context.trace
    assert trace == terminal
    assert len({event["invocation_id"] for event in trace}) == 4
    assert len([event for event in observed if event["kind"] == "evidence"]) == 4
    assert [event["error"] for event in trace if event["kind"] == "trace_unavailable"] == [
        "Agent trace unavailable"
    ]
    assert trace[-1]["trace"]["status"] == "failed"
    assert json.loads(json.dumps(trace)) == trace


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["raise", "ignore"])
async def test_prior_observer_error_policy_is_unchanged(evaluations, monkeypatch, policy):
    class Predictor:
        async def acall(self, *, text):
            return SimpleNamespace(answer=text, trace=Trace(text))

    monkeypatch.setattr(agent_module, "_build_predictor", lambda *a, **kw: Predictor())
    failure = RuntimeError("existing evidence observer failed")

    def observer(event):
        raise failure

    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(*, agent: ava.Agent) -> str:
        return (await agent(text="hello")).answer

    submissions = []
    with capture_agent_evidence(observer, errors=policy):
        with capture_evaluations(submissions.append):
            if policy == "raise":
                with pytest.raises(RuntimeError) as raised:
                    await echo.fn()
                assert raised.value is failure
            else:
                assert await echo.fn() == "hello"

    assert len(submissions) == (0 if policy == "raise" else 1)


@pytest.mark.asyncio
async def test_failed_step_never_submits_evaluations(evaluations, monkeypatch):
    class Predictor:
        async def acall(self, *, text):
            return SimpleNamespace(answer=text, trace=Trace(text))

    monkeypatch.setattr(agent_module, "_build_predictor", lambda *a, **kw: Predictor())
    failure = ValueError("final Python transformation failed")

    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(*, agent: ava.Agent) -> str:
        await agent(text="raw prediction")
        raise failure

    submissions = []
    with capture_evaluations(submissions.append) as errors:
        with pytest.raises(ValueError) as raised:
            await echo.fn()

    assert raised.value is failure
    assert submissions == []
    assert errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_type", [RuntimeError, KeyboardInterrupt, asyncio.CancelledError]
)
async def test_submission_failure_is_reported_without_changing_return(
    evaluations,
    failure_type,
):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    def echo(*, agent: ava.Agent) -> str:
        return "final answer"

    def reject(submission):
        raise failure_type("handoff failed")

    with capture_evaluations(reject) as errors:
        assert await echo.fn() == "final answer"

    assert len(errors) == 1
    assert "handoff failed" in errors[0].error
    assert errors[0].submission.context.output == "final answer"


@pytest.mark.asyncio
async def test_async_collector_does_not_run_without_a_synchronous_snapshot(evaluations):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    def echo(*, agent: ava.Agent) -> str:
        return "unchanged"

    calls = []

    async def invalid_collector(submission):
        calls.append(submission)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with capture_evaluations(invalid_collector) as errors:
            assert await echo.fn() == "unchanged"

    assert calls == []
    assert len(errors) == 1
    assert "synchronous" in errors[0].error


@pytest.mark.asyncio
async def test_warning_and_diagnostic_failures_are_isolated(evaluations, monkeypatch):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    def echo(*, agent: ava.Agent) -> str:
        return "unchanged"

    def reject(submission):
        warnings.warn("snapshot unavailable", UserWarning)

    def broken_diagnostic(*args, **kwargs):
        warnings.warn("diagnostic unavailable", UserWarning)

    monkeypatch.setattr(capture_module.logging.Logger, "warning", broken_diagnostic)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with capture_evaluations(reject) as errors:
            assert await echo.fn() == "unchanged"
        assert await echo.fn() == "unchanged"

    assert len(errors) == 1
    assert "snapshot unavailable" in errors[0].error


@pytest.mark.asyncio
async def test_context_capture_failure_is_submitted_as_evaluation_error(
    evaluations,
    monkeypatch,
):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    def echo(*, agent: ava.Agent) -> str:
        return "unchanged"

    def fail_context(**kwargs):
        raise ValueError("context unavailable")

    monkeypatch.setattr(capture_module, "EvalContext", fail_context)
    submissions = []
    with capture_evaluations(submissions.append):
        assert await echo.fn() == "unchanged"

    assert len(submissions) == 1
    assert submissions[0].context is None
    assert "context unavailable" in submissions[0].error


def test_local_workflows_bind_classifier_defaults_and_exclude_explicit_services(evaluations):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    def echo(text: str, *, logger, agent: ava.Agent) -> str:
        return text

    @ava.workflow(classifier_defaults={"model": "workflow-a", "timeout": 3.0})
    def first():
        return echo("first", logger=ava.Logger())

    @ava.workflow(classifier_defaults={"model": "workflow-b", "timeout": 7.0})
    def second():
        return echo("second", logger=ava.Logger())

    submissions = []
    with capture_evaluations(submissions.append):
        assert first().run(executor=ava.LocalExecutor(max_workers=1)).result() == "first"
        assert second().run(executor=ava.LocalExecutor(max_workers=1)).result() == "second"
        assert asyncio.run(echo.fn("direct", logger=object())) == "direct"

    assert [submission.runtime_defaults for submission in submissions] == [
        {"model": "workflow-a", "timeout": 3.0},
        {"model": "workflow-b", "timeout": 7.0},
        {},
    ]
    assert submissions[0].context.inputs == {"text": "first"}
    assert submissions[1].context.inputs == {"text": "second"}


@pytest.mark.asyncio
async def test_ray_serialization_uses_worker_context_and_synchronous_snapshot(evaluations):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    def echo(text: str, *, agent: ava.Agent) -> list[str]:
        return [text]

    bound = echo.fn.__agent_step__.with_workflow_defaults(
        echo.fn, {}, classifier_defaults={"model": "worker-model", "timeout": 2.0}
    )
    restored = cloudpickle.loads(cloudpickle.dumps(bound))
    snapshots = []

    def snapshot(submission):
        snapshots.append(cloudpickle.dumps(submission))

    with capture_evaluations(snapshot):
        result = await restored("initial")
    result.append("downstream mutation")

    submission = cloudpickle.loads(snapshots[0])
    assert submission.context.output == ["initial"]
    assert submission.context.inputs == {"text": "initial"}
    assert submission.runtime_defaults == {"model": "worker-model", "timeout": 2.0}
