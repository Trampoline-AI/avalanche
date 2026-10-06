"""Evaluation capture owns the first successful agent return, not the step result."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import warnings
from typing import Literal

import pytest
from dspy import Prediction
from predict_rlm import IterationStep, RunEvent, RunEventKind, RunEvidence, RunTrace
from pydantic import BaseModel
from ray import cloudpickle

import avalanche as ava
from avalanche._agent_evidence import capture_agent_evidence, emit_agent_evidence
from avalanche.agent import AgentStepExecutionError
from avalanche.evaluation_capture import capture_evaluations

agent_module = importlib.import_module("avalanche.agent.agent_step")
capture_module = importlib.import_module("avalanche.evaluation_capture")


class Report(BaseModel):
    summary: str


class EchoSignature(ava.Signature):
    text: str = ava.InputField()
    answer: str = ava.OutputField()
    handoff: Report = ava.OutputField()


class SnapshotSignature(ava.Signature):
    payload: list[str] = ava.InputField()
    answer: str = ava.OutputField()
    handoff: Report = ava.OutputField()


def _trace(
    text: str,
    status: Literal["in_progress", "completed", "max_iterations", "error"] = "completed",
) -> RunTrace:
    return RunTrace(
        status=status,
        model="test-model",
        iterations=1,
        max_iterations=2,
        duration_ms=12,
        steps=[
            IterationStep(
                iteration=1,
                reasoning=f"Process {text}",
                code=f"answer = {text!r}",
                output=text,
                untruncated_output=f"full output: {text}",
                duration_ms=12,
            )
        ],
    )


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


@pytest.fixture
def predictor(monkeypatch):
    class Predictor:
        async def acall(self, *, text):
            return Prediction(
                answer=text.upper(),
                handoff=Report(summary=f"summary: {text}"),
                trace=_trace(text),
                evidence=RunEvidence(run_id=text, complete=True, terminal_outcome="completed"),
            )

    monkeypatch.setattr(agent_module, "_build_predictor", lambda *a, **kw: Predictor())


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


@pytest.mark.asyncio
async def test_embedded_execution_does_not_run_evaluations(evaluations, predictor):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(*, agent: ava.Agent) -> str:
        return (await agent(text="hello")).answer

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert await echo.fn() == "HELLO"


@pytest.mark.asyncio
async def test_actual_call_inputs_and_complete_prediction_replace_step_result(
    evaluations,
    predictor,
):
    submissions = []

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
        prediction = await agent(text=f"prepared: {text.strip()}")
        # Submission must already exist before postprocessing starts.
        assert len(submissions) == 1
        return Report(summary=" ".join([prediction.answer] * repeat))

    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with capture_evaluations(submissions.append) as errors:
            result = await echo.fn(" hello ", context=object())

    assert result == Report(summary="PREPARED: HELLO PREPARED: HELLO")
    assert errors == []
    context = submissions[0].context
    assert submissions[0].error is None
    assert context.inputs == {"text": "prepared: hello"}
    assert isinstance(context.output, Prediction)
    assert context.output.answer == "PREPARED: HELLO"
    assert context.output.handoff == Report(summary="summary: prepared: hello")
    assert observed[0]["trace"]["steps"] == []
    assert context.trace[0]["invocation_id"] == observed[0]["invocation_id"]
    selected_trace = RunTrace.model_validate(context.trace[0]["trace"], strict=True)
    assert selected_trace.steps == [
        IterationStep(
            iteration=1,
            reasoning="Process prepared: hello",
            code="answer = 'prepared: hello'",
            output="prepared: hello",
            untruncated_output="full output: prepared: hello",
            duration_ms=12,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("helper_evaluated", [False, True])
@pytest.mark.parametrize("failure_type", [None, ValueError, asyncio.CancelledError])
async def test_nested_step_capture_is_isolated_and_restored(
    evaluations,
    predictor,
    helper_evaluated,
    failure_type,
):
    helper_evaluations = (
        ava.Evaluations(metrics={"helper_quality": evaluations.metrics["quality"]})
        if helper_evaluated
        else None
    )
    failure = failure_type("helper postprocessing failed") if failure_type else None

    @ava.agent_step(EchoSignature, evaluations=helper_evaluations)
    async def helper(*, agent: ava.Agent) -> str:
        prediction = await agent(text="helper")
        if failure is not None:
            raise failure
        return prediction.answer

    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def outer(*, agent: ava.Agent) -> str:
        if failure_type is None:
            assert await helper.fn() == "HELPER"
        else:
            with pytest.raises(failure_type) as raised:
                await helper.fn()
            assert raised.value is failure
        return (await agent(text="outer")).answer

    submissions = []
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with capture_evaluations(submissions.append) as errors:
            result = await outer.fn()

    assert result == "OUTER"
    assert errors == []
    assert [submission.evaluations for submission in submissions] == (
        [helper_evaluations, evaluations] if helper_evaluated else [evaluations]
    )
    terminals = [event for event in observed if event["kind"] != "evidence"]
    assert len(terminals) == 2
    expected_calls = [("helper", terminals[0]), ("outer", terminals[1])]
    if not helper_evaluated:
        expected_calls = expected_calls[1:]
    for submission, (text, terminal) in zip(submissions, expected_calls, strict=True):
        assert submission.error is None
        context = submission.context
        assert context.inputs == {"text": text}
        assert context.output.answer == text.upper()
        assert context.output.handoff == Report(summary=f"summary: {text}")
        assert context.output.evidence.run_id == text
        assert context.trace[0]["invocation_id"] == terminal["invocation_id"]
        selected_trace = RunTrace.model_validate(context.trace[0]["trace"], strict=True)
        assert selected_trace == _trace(text)


@pytest.mark.asyncio
async def test_unevaluated_helper_does_not_create_evaluation_for_no_call_outer(
    evaluations, predictor
):
    @ava.agent_step(EchoSignature)
    async def helper(*, agent: ava.Agent) -> str:
        return (await agent(text="helper")).answer

    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def outer(*, agent: ava.Agent) -> str:
        return await helper.fn()

    submissions = []
    with capture_evaluations(submissions.append) as errors:
        assert await outer.fn() == "HELPER"

    assert submissions == []
    assert errors == []


@pytest.mark.asyncio
async def test_first_successful_completion_wins_without_gating_later_calls(
    evaluations,
    monkeypatch,
):
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    finished = []

    class Predictor:
        async def acall(self, *, text):
            if text == "slow":
                slow_started.set()
                await release_slow.wait()
            else:
                await slow_started.wait()
            finished.append(text)
            return Prediction(
                answer=text.upper(),
                handoff=Report(summary=text),
                trace=_trace(text),
                evidence=RunEvidence(run_id=text, complete=True, terminal_outcome="completed"),
            )

    monkeypatch.setattr(agent_module, "_build_predictor", lambda *a, **kw: Predictor())
    submissions = []

    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(*, agent: ava.Agent) -> list[str]:
        slow = asyncio.create_task(agent(text="slow"))
        fast = await agent(text="fast")
        assert len(submissions) == 1
        assert submissions[0].context.output.answer == "FAST"
        release_slow.set()
        later = await slow
        return [later.answer, fast.answer]

    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with capture_evaluations(submissions.append) as errors:
            result = await echo.fn()

    assert result == ["SLOW", "FAST"]
    assert finished == ["fast", "slow"]
    assert errors == []
    assert len(submissions) == 1
    context = submissions[0].context
    assert context.inputs == {"text": "fast"}
    assert context.output.handoff.summary == "fast"
    terminals = [event for event in observed if event["kind"] != "evidence"]
    assert len(terminals) == 2
    assert all(event["trace"]["steps"] == [] for event in terminals)
    assert context.trace[0]["invocation_id"] == terminals[0]["invocation_id"]
    selected_trace = RunTrace.model_validate(context.trace[0]["trace"], strict=True)
    assert selected_trace.steps == _trace("fast").steps
    assert context.trace[0]["invocation_id"] != terminals[1]["invocation_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exportable_failure_trace", [False, True])
async def test_failed_earlier_call_does_not_claim_capture_and_observers_keep_all_calls(
    evaluations,
    monkeypatch,
    exportable_failure_trace,
):
    class Predictor:
        async def acall(self, *, text):
            sink = agent_module._AvalancheEvidenceSink()
            await sink.emit(
                RunEvent(text, 1, RunEventKind.RUN_STARTED, 1, {"inputs": {"text": text}})
            )
            if text == "failed":
                failure = ValueError("provider failed")
                failure.evidence = RunEvidence(
                    run_id=text, complete=True, terminal_outcome="error"
                )
                if exportable_failure_trace:
                    failure.trace = _trace(text, status="error")
                raise failure
            return Prediction(
                answer=text,
                handoff=Report(summary=text),
                trace=_trace(text),
                evidence=RunEvidence(run_id=text, complete=True, terminal_outcome="completed"),
            )

    monkeypatch.setattr(agent_module, "_build_predictor", lambda *a, **kw: Predictor())

    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(*, agent: ava.Agent) -> list[str]:
        with pytest.raises(AgentStepExecutionError):
            await agent(text="failed")
        first = await agent(text="first")
        later = await agent(text="later")
        return [first.answer, later.answer]

    observed = []
    submissions = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with capture_evaluations(submissions.append):
            assert await echo.fn() == ["first", "later"]
        emit_agent_evidence(
            {
                "kind": "trace_unavailable",
                "invocation_id": "outside",
                "error": "outside",
                "evidence": {
                    "run_id": "outside",
                    "complete": True,
                    "terminal_outcome": "error",
                },
            }
        )

    assert observed[-1]["invocation_id"] == "outside"
    terminal = [event for event in observed[:-1] if event["kind"] != "evidence"]
    assert len(terminal) == 3
    assert terminal[0]["evidence"]["terminal_outcome"] == "error"
    if exportable_failure_trace:
        assert terminal[0]["trace"]["status"] == "error"
        assert terminal[0]["trace"]["steps"] == []
    else:
        assert terminal[0]["kind"] == "trace_unavailable"
    assert len([event for event in observed if event["kind"] == "evidence"]) == 3
    assert len(submissions) == 1
    context = submissions[0].context
    assert context.inputs == {"text": "first"}
    assert context.output.answer == "first"
    assert context.trace[0]["invocation_id"] == terminal[1]["invocation_id"]
    assert all(event["trace"]["steps"] == [] for event in terminal[1:])
    selected_trace = RunTrace.model_validate(context.trace[0]["trace"], strict=True)
    assert selected_trace.steps == _trace("first").steps
    assert json.loads(json.dumps(context.trace)) == context.trace


def test_unavailable_terminal_trace_is_retained_at_capture_boundary(evaluations):
    output = Prediction(
        answer="first",
        handoff=Report(summary="first"),
        trace=_trace("first"),
        evidence=RunEvidence(run_id="first", complete=True, terminal_outcome="completed"),
    )
    submissions = []
    with capture_evaluations(submissions.append) as errors:
        capture = capture_module._StepEvaluationCapture(evaluations, step_name="echo")
        capture.submit(
            {"text": "first"},
            output,
            {
                "kind": "trace_unavailable",
                "invocation_id": "first-invocation",
                "error": "terminal trace unavailable",
                "evidence": {
                    "run_id": "first",
                    "complete": True,
                    "terminal_outcome": "completed",
                },
            },
        )

    assert errors == []
    assert submissions[0].error is None
    assert submissions[0].context.inputs == {"text": "first"}
    assert submissions[0].context.output.answer == "first"
    assert submissions[0].context.trace == [
        {
            "kind": "trace_unavailable",
            "invocation_id": "first-invocation",
            "error": "terminal trace unavailable",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["raise", "ignore"])
async def test_prior_observer_error_policy_is_unchanged(evaluations, predictor, policy):
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
                assert await echo.fn() == "HELLO"

    assert len(submissions) == (0 if policy == "raise" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [ValueError, asyncio.CancelledError])
async def test_step_postprocessing_failure_does_not_erase_agent_capture(
    evaluations,
    predictor,
    failure_type,
):
    failure = failure_type("final Python transformation failed")

    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(*, agent: ava.Agent) -> str:
        await agent(text="raw prediction")
        raise failure

    submissions = []
    with capture_evaluations(submissions.append) as errors:
        with pytest.raises(failure_type) as raised:
            await echo.fn()

    assert raised.value is failure
    assert errors == []
    assert len(submissions) == 1
    assert submissions[0].context.output.answer == "RAW PREDICTION"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["no-call", "step-error", "agent-error", "cancelled-agent"])
async def test_no_successful_agent_return_means_no_evaluation(evaluations, monkeypatch, mode):
    class Predictor:
        async def acall(self, *, text):
            if mode == "cancelled-agent":
                failure = asyncio.CancelledError("cancelled before prediction")
                outcome = "cancelled"
            else:
                failure = ValueError("provider failed")
                outcome = "error"
            failure.evidence = RunEvidence(run_id=text, complete=True, terminal_outcome=outcome)
            raise failure

    monkeypatch.setattr(agent_module, "_build_predictor", lambda *a, **kw: Predictor())

    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(*, agent: ava.Agent) -> str:
        if mode == "step-error":
            raise ValueError("before agent")
        if mode in ("agent-error", "cancelled-agent"):
            await agent(text="hello")
        return "no prediction"

    submissions = []
    with capture_evaluations(submissions.append) as errors:
        if mode == "no-call":
            assert await echo.fn() == "no prediction"
        else:
            expected = {
                "step-error": ValueError,
                "agent-error": AgentStepExecutionError,
                "cancelled-agent": asyncio.CancelledError,
            }[mode]
            with pytest.raises(expected):
                await echo.fn()
    assert submissions == []
    assert errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_type", [RuntimeError, KeyboardInterrupt, asyncio.CancelledError]
)
async def test_submission_failure_is_reported_without_changing_agent_or_step_return(
    evaluations,
    predictor,
    failure_type,
):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(*, agent: ava.Agent) -> list[str]:
        first = await agent(text="first")
        later = await agent(text="later")
        return [first.answer, later.answer]

    attempts = []

    def reject(submission):
        attempts.append(submission)
        raise failure_type("handoff failed")

    with capture_evaluations(reject) as errors:
        assert await echo.fn() == ["FIRST", "LATER"]
    assert len(attempts) == 1
    assert len(errors) == 1
    assert "handoff failed" in errors[0].error
    assert errors[0].submission.context.output.answer == "FIRST"


@pytest.mark.asyncio
async def test_async_collector_does_not_run_without_a_synchronous_snapshot(
    evaluations, predictor
):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(*, agent: ava.Agent) -> str:
        return (await agent(text="unchanged")).answer

    calls = []

    async def invalid_collector(submission):
        calls.append(submission)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with capture_evaluations(invalid_collector) as errors:
            assert await echo.fn() == "UNCHANGED"

    assert calls == []
    assert len(errors) == 1
    assert "synchronous" in errors[0].error


@pytest.mark.asyncio
async def test_warning_and_diagnostic_failures_are_isolated(
    evaluations, predictor, monkeypatch
):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(*, agent: ava.Agent) -> str:
        return (await agent(text="unchanged")).answer

    def reject(submission):
        warnings.warn("snapshot unavailable", UserWarning)

    def broken_diagnostic(*args, **kwargs):
        warnings.warn("diagnostic unavailable", UserWarning)

    monkeypatch.setattr(capture_module.logging.Logger, "warning", broken_diagnostic)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with capture_evaluations(reject) as errors:
            assert await echo.fn() == "UNCHANGED"
        assert await echo.fn() == "UNCHANGED"

    assert len(errors) == 1
    assert "snapshot unavailable" in errors[0].error


@pytest.mark.asyncio
async def test_context_capture_failure_is_reported_once_without_retry(
    evaluations, predictor, monkeypatch
):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(*, agent: ava.Agent) -> list[str]:
        return [(await agent(text=text)).answer for text in ("first", "later")]

    def fail_context(**kwargs):
        raise ValueError("context unavailable")

    monkeypatch.setattr(capture_module, "EvalContext", fail_context)
    submissions = []
    with capture_evaluations(submissions.append):
        assert await echo.fn() == ["FIRST", "LATER"]

    assert len(submissions) == 1
    assert submissions[0].context is None
    assert "context unavailable" in submissions[0].error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_type", [ValueError, KeyboardInterrupt, asyncio.CancelledError]
)
async def test_full_trace_export_failure_is_reported_once_without_changing_returns(
    evaluations, predictor, monkeypatch, failure_type
):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(*, agent: ava.Agent) -> list[str]:
        return [(await agent(text=text)).answer for text in ("first", "later")]

    original_export = RunTrace.to_exportable_json
    exports = []

    def fail_full_export(self, *args, **kwargs):
        if self.steps:
            exports.append(self.steps[0].output)
            raise failure_type("full trace unavailable")
        return original_export(self, *args, **kwargs)

    monkeypatch.setattr(RunTrace, "to_exportable_json", fail_full_export)
    submissions = []
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with capture_evaluations(submissions.append) as errors:
            assert await echo.fn() == ["FIRST", "LATER"]

    assert exports == ["first"]
    assert errors == []
    assert len(submissions) == 1
    assert submissions[0].context is None
    assert submissions[0].error == f"{failure_type.__name__}: full trace unavailable"
    assert len(observed) == 2
    assert all(event["trace"]["steps"] == [] for event in observed)


def test_local_workflows_use_own_classifier_defaults_and_actual_call_inputs(
    evaluations, predictor
):
    @ava.agent_step(EchoSignature, evaluations=evaluations)
    async def echo(text: str, *, logger, agent: ava.Agent) -> str:
        return (await agent(text=f"prepared {text}")).answer

    @ava.workflow(classifier_defaults={"model": "workflow-a", "timeout": 3.0})
    def first():
        return echo("first", logger=ava.Logger())

    @ava.workflow(classifier_defaults={"model": "workflow-b", "timeout": 7.0})
    def second():
        return echo("second", logger=ava.Logger())

    submissions = []
    with capture_evaluations(submissions.append):
        assert (
            first().run(executor=ava.LocalExecutor(max_workers=1)).result() == "PREPARED FIRST"
        )
        assert (
            second().run(executor=ava.LocalExecutor(max_workers=1)).result()
            == "PREPARED SECOND"
        )
        assert asyncio.run(echo.fn("direct", logger=object())) == "PREPARED DIRECT"

    assert [submission.runtime_defaults for submission in submissions] == [
        {"model": "workflow-a", "timeout": 3.0},
        {"model": "workflow-b", "timeout": 7.0},
        {},
    ]
    assert [submission.context.inputs for submission in submissions] == [
        {"text": "prepared first"},
        {"text": "prepared second"},
        {"text": "prepared direct"},
    ]


@pytest.mark.asyncio
async def test_ray_serialization_uses_worker_context_and_synchronous_owned_snapshot(
    evaluations,
    monkeypatch,
):
    class Predictor:
        async def acall(self, *, payload):
            return Prediction(
                answer=payload[0].upper(),
                handoff=Report(summary="original summary"),
                sources=["original source"],
                trace=_trace(payload[0]),
                evidence=RunEvidence(
                    run_id="snapshot-run", complete=True, terminal_outcome="completed"
                ),
            )

    monkeypatch.setattr(agent_module, "_build_predictor", lambda *a, **kw: Predictor())

    @ava.agent_step(SnapshotSignature, evaluations=evaluations)
    async def echo(text: str, *, agent: ava.Agent) -> Prediction:
        payload = [f"prepared {text}"]
        prediction = await agent(payload=payload)
        payload.append("caller mutation")
        prediction.handoff.summary = "caller changed summary"
        prediction.sources.append("caller source")
        prediction.trace.steps[0].output = "caller changed trace"
        prediction.evidence.complete = False
        return prediction

    bound = echo.fn.__agent_step__.with_workflow_defaults(
        echo.fn, {}, classifier_defaults={"model": "worker-model", "timeout": 2.0}
    )
    restored = cloudpickle.loads(cloudpickle.dumps(bound))
    snapshots = []

    def snapshot(submission):
        snapshots.append(cloudpickle.dumps(submission))

    with capture_evaluations(snapshot):
        result = await restored("initial")
    result.answer = "downstream mutation"

    # The synchronous worker collector snapshots before caller/downstream mutation.
    submission = cloudpickle.loads(snapshots[0])
    assert result.handoff.summary == "caller changed summary"
    assert result.trace.steps[0].output == "caller changed trace"
    assert result.evidence.complete is False
    assert submission.context.output.answer == "PREPARED INITIAL"
    assert submission.context.output.handoff.summary == "original summary"
    assert submission.context.output.sources == ["original source"]
    assert submission.context.inputs == {"payload": ["prepared initial"]}
    assert submission.context.output.trace.steps == _trace("prepared initial").steps
    assert submission.context.output.evidence.complete is True
    selected_trace = RunTrace.model_validate(submission.context.trace[0]["trace"], strict=True)
    assert selected_trace.steps == _trace("prepared initial").steps
    assert submission.runtime_defaults == {"model": "worker-model", "timeout": 2.0}
