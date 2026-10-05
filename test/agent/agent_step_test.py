"""Agent invocation, evidence isolation, and failure boundaries without network calls."""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
from types import SimpleNamespace
from typing import Literal

import pytest
from predict_rlm import RunEvidence, RunEvidenceEvent, RunTrace
from pydantic import BaseModel

import avalanche as ava
from avalanche.agent import AgentStepError, AgentStepExecutionError, capture_agent_evidence

agent_module = importlib.import_module("avalanche.agent.agent_step")


class Person(BaseModel):
    id: int
    name: str


class Summary(BaseModel):
    headline: str
    person_count: int


class SummarySignature(ava.Signature):
    person: Person = ava.InputField()
    summary: Summary = ava.OutputField()
    note: str = ava.OutputField()


@pytest.mark.parametrize(
    "signature",
    [
        SummarySignature,
        ava.Signature(
            "person: Person -> summary: Summary, note: str",
            "Summarize the person and provide a review note.",
            custom_types={"Person": Person, "Summary": Summary},
        ),
    ],
    ids=["class", "inline"],
)
def test_bodyful_agent_invokes_service_and_owns_structured_result(monkeypatch, signature):
    calls = []

    class Predictor:
        async def acall(self, **inputs):
            person = inputs["person"]
            calls.append(person)
            return SimpleNamespace(
                summary=Summary(headline=f"about {person.name}", person_count=1),
                note=f"review {person.id}",
                trace=_trace(),
                evidence=RunEvidence(
                    run_id="summary-run", complete=True, terminal_outcome="completed"
                ),
            )

    monkeypatch.setattr(agent_module, "_build_predictor", lambda *args, **kwargs: Predictor())

    @ava.source
    def load():
        return Person(id=1, name="Ada")

    @ava.agent_step(signature)
    async def summarize(person: Person, *, agent: ava.Agent):
        prediction = await agent(person=person)
        return {"headline": prediction.summary.headline, "note": prediction.note}

    @ava.workflow
    def flow():
        return summarize(load())

    assert flow().run(executor=ava.LocalExecutor()).result() == {
        "headline": "about Ada",
        "note": "review 1",
    }
    assert calls == [Person(id=1, name="Ada")]


@pytest.mark.asyncio
async def test_agent_rejects_bad_inputs_before_invocation_and_preserves_service_failure(
    monkeypatch,
):
    calls = []
    failure = ValueError("provider unavailable")
    failure.evidence = RunEvidence(run_id="failed-run", complete=True, terminal_outcome="error")

    class Predictor:
        async def acall(self, **inputs):
            calls.append(inputs)
            raise failure

    monkeypatch.setattr(agent_module, "_build_predictor", lambda *args, **kwargs: Predictor())
    agent = ava.Agent(signature=SummarySignature, step_name="summarize", runtime_kwargs={})
    with pytest.raises(AgentStepError):
        await agent(extra="not in signature")
    assert calls == []
    person = Person(id=1, name="Ada")
    with pytest.raises(AgentStepExecutionError) as raised:
        await agent(person=person)
    assert raised.value.__cause__ is failure
    assert calls == [{"person": person}]


def _trace(
    status: Literal["in_progress", "completed", "max_iterations", "error"] = "completed",
) -> RunTrace:
    return RunTrace(
        status=status,
        model="test-model",
        iterations=0,
        max_iterations=1,
        duration_ms=1,
    )


@pytest.mark.asyncio
async def test_live_evidence_redacts_tool_and_model_secrets_without_losing_event_order():
    from predict_rlm import RunEvent, RunEventKind

    class Predictor:
        async def acall(self, **inputs):
            sink = agent_module._AvalancheEvidenceSink()
            events = [
                (RunEventKind.RUN_STARTED, {"inputs": inputs}),
                (
                    RunEventKind.PREDICT_STARTED,
                    {
                        "call_id": "p",
                        "signature": "question -> answer",
                        "instructions": None,
                        "model": "test-model",
                        "input": "model-secret",
                    },
                ),
                (RunEventKind.PREDICT_FINISHED, {"call_id": "p", "output": "model-secret"}),
                (
                    RunEventKind.TOOL_STARTED,
                    {"call_id": "t", "name": "lookup", "args": ["tool-secret"]},
                ),
                (
                    RunEventKind.TOOL_FINISHED,
                    {"call_id": "t", "name": "lookup", "result": "tool-secret"},
                ),
                (
                    RunEventKind.RUN_SUCCEEDED,
                    {"status": "completed", "outputs": {"note": "reviewed"}},
                ),
            ]
            for sequence, (kind, data) in enumerate(events, 1):
                await sink.emit(RunEvent("run", sequence, kind, sequence, data))
            return SimpleNamespace(
                note="reviewed",
                trace=_trace(),
                evidence=RunEvidence(
                    run_id="run",
                    complete=True,
                    terminal_outcome="completed",
                    events=[
                        RunEvidenceEvent(
                            sequence=1,
                            kind="tool.finished",
                            timestamp_ns=1,
                            data={"result": "tool-secret"},
                        )
                    ],
                ),
            )

    agent = ava.Agent(signature=SummarySignature, step_name="summarize", runtime_kwargs={})
    agent._predictor = Predictor()
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await agent(person=Person(id=1, name="Ada"))

    assert result.note == "reviewed"
    live = observed[:-1]
    assert [event["sequence"] for event in live] == list(range(1, 7))
    assert live[0]["data"]["inputs"] == {"person": {"id": 1, "name": "Ada"}}
    assert live[-1]["data"]["outputs"] == {"note": "reviewed"}
    assert "model-secret" not in json.dumps(observed)
    assert "tool-secret" not in json.dumps(observed)
    assert observed[-1]["kind"] == "trace_finished"
    assert observed[-1]["evidence"] == {
        "run_id": "run",
        "complete": True,
        "terminal_outcome": "completed",
    }
    assert "evidence" not in observed[-1]["trace"]
    assert len({event["invocation_id"] for event in observed}) == 1


@pytest.mark.asyncio
async def test_concurrent_agent_invocations_keep_evidence_and_traces_correlated():
    from predict_rlm import RunEvent, RunEventKind

    class Predictor:
        def __init__(self):
            self.ready = asyncio.Event()
            self.arrivals = 0

        async def acall(self, **inputs):
            name = inputs["person"].name
            sink = agent_module._AvalancheEvidenceSink()
            await sink.emit(
                RunEvent(
                    name,
                    1,
                    RunEventKind.PREDICT_STARTED,
                    1,
                    {
                        "call_id": name,
                        "invocation_id": f"caller-{name}",
                        "signature": "question -> answer",
                        "instructions": None,
                        "model": "test-model",
                    },
                )
            )
            self.arrivals += 1
            if self.arrivals == 2:
                self.ready.set()
            await asyncio.wait_for(self.ready.wait(), timeout=5)
            await sink.close(
                name,
                RunEvent(
                    name,
                    2,
                    RunEventKind.RUN_SUCCEEDED,
                    2,
                    {"status": "completed", "outputs": {"note": name}},
                ),
            )
            return SimpleNamespace(
                note=name,
                trace=_trace(),
                evidence=RunEvidence(run_id=name, complete=True, terminal_outcome="completed"),
            )

    agent = ava.Agent(signature=SummarySignature, step_name="summarize", runtime_kwargs={})
    agent._predictor = Predictor()
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        left, right = await asyncio.gather(
            agent(person=Person(id=1, name="left")),
            agent(person=Person(id=2, name="right")),
        )
    assert (left.note, right.note) == ("left", "right")
    grouped = {}
    for event in observed:
        grouped.setdefault(event["invocation_id"], []).append(event)
    assert len(grouped) == 2
    assert not set(grouped) & {"left", "right", "caller-left", "caller-right"}
    for events in grouped.values():
        assert [event["kind"] for event in events] == ["evidence", "evidence", "trace_finished"]
        assert events[0]["data"]["call_id"] == events[-1]["evidence"]["run_id"]
        assert "invocation_id" not in events[0]["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["persistence", "interrupt", "cancel"])
async def test_real_recorder_propagates_strict_observer_failures(failure_kind):
    from predict_rlm import EvidenceIncompleteError, EvidenceRecorder, RunContext, RunEventKind

    predictor = agent_module._build_predictor(SummarySignature, skills=(), tools=())
    failures = {
        "persistence": RuntimeError("persistence failed"),
        "interrupt": KeyboardInterrupt("listener interrupted"),
        "cancel": asyncio.CancelledError("listener cancelled"),
    }
    failure = failures[failure_kind]

    class RecorderPredictor:
        async def acall(self, **inputs):
            recorder = EvidenceRecorder(
                RunContext(predictor.runtime_spec, inputs), predictor.runtime_spec.events
            )
            try:
                await recorder.emit(RunEventKind.RUN_STARTED, inputs=inputs)
            except BaseException as error:
                await recorder.finish_failure(error)
                predictor._attach_runtime_evidence(error, recorder)
                raise
            result = SimpleNamespace(note="recorded", trace=_trace())
            await recorder.finish_success(status="completed", outputs={"note": "recorded"})
            predictor._attach_runtime_evidence(result, recorder)
            return result

    def broken_listener(event):
        if event["kind"] == "evidence":
            raise failure

    agent = ava.Agent(signature=SummarySignature, step_name="summarize", runtime_kwargs={})
    agent._predictor = RecorderPredictor()
    expected = AgentStepExecutionError if failure_kind == "persistence" else type(failure)
    with capture_agent_evidence(broken_listener, errors="raise"):
        with pytest.raises(expected) as raised:
            await agent(person=Person(id=1, name="Ada"))
    if failure_kind == "persistence":
        assert isinstance(raised.value.__cause__, EvidenceIncompleteError)
        assert raised.value.__cause__.__cause__ is failure
        with capture_agent_evidence(broken_listener, errors="ignore"):
            assert (await agent(person=Person(id=1, name="Ada"))).note == "recorded"
    else:
        assert raised.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("exportable_trace", [False, True])
async def test_agent_cancellation_emits_one_terminal_and_preserves_cancellation(
    exportable_trace,
):
    cancellation = asyncio.CancelledError("cancelled by caller")
    cancellation.evidence = RunEvidence(
        run_id="cancelled-run", complete=True, terminal_outcome="cancelled"
    )
    if exportable_trace:
        cancellation.trace = _trace(status="error")

    class Predictor:
        async def acall(self, **inputs):
            raise cancellation

    agent = ava.Agent(signature=SummarySignature, step_name="summarize", runtime_kwargs={})
    agent._predictor = Predictor()
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(asyncio.CancelledError) as raised:
            await agent(person=Person(id=1, name="Ada"))
    assert raised.value is cancellation
    assert len(observed) == 1
    assert observed[0]["evidence"] == {
        "run_id": "cancelled-run",
        "complete": True,
        "terminal_outcome": "cancelled",
    }
    if exportable_trace:
        assert observed[0]["kind"] == "trace_finished"
        assert observed[0]["trace"]["status"] == "error"
    else:
        assert observed[0]["kind"] == "trace_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["start", "end"])
async def test_dspy_callback_cancellation_without_sdk_evidence_preserves_cancelled_task(phase):
    import dspy
    from dspy.utils.callback import BaseCallback

    cancellation = asyncio.CancelledError("cancelled by callback")

    class CancelCallback(BaseCallback):
        def on_module_start(self, call_id, instance, inputs):
            if phase == "start":
                raise cancellation

        def on_module_end(self, call_id, outputs, exception=None):
            if phase == "end":
                raise cancellation

    class Predictor(dspy.Module):
        async def aforward(self, person):
            return dspy.Prediction(note="completed")

    agent = ava.Agent(signature=SummarySignature, step_name="summarize", runtime_kwargs={})
    agent._predictor = Predictor(callbacks=[CancelCallback()])
    task = asyncio.create_task(agent(person=Person(id=1, name="Ada")))
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert raised.value is cancellation
    assert task.cancelled()


@pytest.mark.asyncio
async def test_terminal_persistence_failure_is_not_reclassified_as_agent_failure():
    class Predictor:
        async def acall(self, **inputs):
            return SimpleNamespace(
                trace=_trace(),
                evidence=RunEvidence(
                    run_id="terminal-run", complete=True, terminal_outcome="completed"
                ),
            )

    agent = ava.Agent(signature=SummarySignature, step_name="summarize", runtime_kwargs={})
    agent._predictor = Predictor()
    failure = RuntimeError("terminal persistence failed")
    observed = []

    def persist_then_fail(event):
        observed.append(event)
        raise failure

    with capture_agent_evidence(persist_then_fail, errors="raise"):
        with pytest.raises(RuntimeError) as raised:
            await agent(person=Person(id=1, name="Ada"))
    assert raised.value is failure
    assert [event["kind"] for event in observed] == ["trace_finished"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exportable_trace", [False, True])
async def test_agent_failure_retains_separate_evidence_without_exposing_raw_events(
    exportable_trace,
):
    failure = ValueError("provider unavailable")
    failure.evidence = RunEvidence(
        run_id="failed-run",
        complete=False,
        terminal_outcome="error",
        events=[
            RunEvidenceEvent(
                sequence=1,
                kind="predict.started",
                timestamp_ns=1,
                data={"input": "private-model-input"},
            )
        ],
    )
    if exportable_trace:
        failure.trace = _trace(status="error")

    class Predictor:
        async def acall(self, **inputs):
            raise failure

    agent = ava.Agent(signature=SummarySignature, step_name="summarize", runtime_kwargs={})
    agent._predictor = Predictor()
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AgentStepExecutionError) as raised:
            await agent(person=Person(id=1, name="Ada"))

    assert raised.value.__cause__ is failure
    assert len(observed) == 1
    assert observed[0]["kind"] == (
        "trace_finished" if exportable_trace else "trace_unavailable"
    )
    assert observed[0]["evidence"] == {
        "run_id": "failed-run",
        "complete": False,
        "terminal_outcome": "error",
    }
    if exportable_trace:
        assert observed[0]["trace"]["status"] == "error"
    assert "private-model-input" not in json.dumps(observed)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["trace", "evidence"])
async def test_successful_agent_prediction_requires_sdk_trace_and_evidence(missing_field):
    prediction = SimpleNamespace(
        note="reviewed",
        trace=_trace(),
        evidence=RunEvidence(
            run_id="contract-run", complete=True, terminal_outcome="completed"
        ),
    )
    delattr(prediction, missing_field)

    class Predictor:
        async def acall(self, **inputs):
            return prediction

    agent = ava.Agent(signature=SummarySignature, step_name="summarize", runtime_kwargs={})
    agent._predictor = Predictor()
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AttributeError):
            await agent(person=Person(id=1, name="Ada"))
    assert observed == []


def _iteration_with_details(payloads):
    from predict_rlm import IterationStep
    from predict_rlm.trace import PredictCallDetail, PredictCallGroup, TokenUsage

    return IterationStep(
        iteration=1,
        reasoning="Inspect the calls.",
        code="answer = predict(question)",
        output="answer ready",
        untruncated_output="answer ready",
        duration_ms=12,
        predict_calls=[
            PredictCallGroup(
                signature="question -> answer",
                model="test-model",
                calls=[
                    PredictCallDetail(
                        duration_ms=3,
                        input={"question": payload},
                        output={"answer": "ready"},
                        usage=TokenUsage(input_tokens=10, output_tokens=2),
                    )
                    for payload in payloads
                ],
            )
        ],
    )


def _retain_iteration(step):
    from predict_rlm import IterationStep, RunEvent, RunEventKind

    from runtime.operator import Operator
    from runtime.operator.models import AgentEventAppended, NodeState, NodeStatus, RunState

    event = agent_module._project_evidence_event(
        RunEvent("sdk-run", 1, RunEventKind.ITERATION_RECORDED, 1, {"step": step}),
        invocation_id="invocation",
    )
    IterationStep.model_validate(event["data"]["step"], strict=True)
    operator = Operator([], watch=False, schedule=False)
    run = RunState(run_id="bounded-detail-run", flow_name="agent-flow")
    run.nodes["agent"] = NodeState("agent", "agent", "step", status=NodeStatus.RUNNING)
    operator._runs[run.run_id] = run
    operator._notify_run(run)
    handle = SimpleNamespace(
        cancel_event=threading.Event(), result_bundle=None, success_quiesced=False
    )
    subscription = operator.subscribe_operator_updates(
        operator.operator_instance_id, operator.current_sequence
    )
    expected_counts = (
        len(step.tool_calls),
        sum(len(group.calls) for group in step.predict_calls),
    )
    original_trace = _trace().model_copy(update={"iterations": 1, "steps": [step]})
    terminal_events = []
    with capture_agent_evidence(terminal_events.append, errors="raise"):
        agent_module._emit_terminal_trace(
            original_trace,
            invocation_id="invocation",
            evidence=agent_module._evidence_metadata(
                RunEvidence(run_id="sdk-run", complete=True, terminal_outcome="completed")
            ),
        )
    try:
        for evidence in (
            event,
            *terminal_events,
        ):
            operator._apply_event(
                run.run_id,
                handle,
                {"type": "agent_evidence", "node_id": "agent", "event": evidence},
            )
            if evidence["kind"] == "evidence":
                while True:
                    update = subscription.get(timeout=5).update
                    if isinstance(update.change, AgentEventAppended):
                        break
                summary = update.change.event
                assert (summary.tool_count, summary.predict_count) == expected_counts
                assert (summary.iteration, summary.duration_ms, summary.error) == (
                    step.iteration,
                    step.duration_ms,
                    step.error,
                )
        snapshot = operator.get_latest_run_snapshot(
            run.run_id,
            operator_instance_id=operator.operator_instance_id,
        )
        page = operator.list_agent_events(page_token=snapshot.nodes[0].event_page_token)
        summary = page.events[0]
        assert (summary.tool_count, summary.predict_count) == expected_counts
        detail = json.loads(operator.get_run(run.run_id).nodes["agent"].agent_trace_json)
        hydrated = RunTrace.model_validate(detail["trace"], strict=True)
        assert len(hydrated.steps) == 1
        assert detail["evidence"]["terminal_outcome"] == "completed"
        assert original_trace.steps[0] is step
        return hydrated.steps[0], detail["events"][0]["data"]
    finally:
        operator.unsubscribe_operator_updates(subscription)
        operator.close()


@pytest.mark.parametrize(
    "payloads",
    [
        ["small question"],
        ["x" * (9 * 1024 * 1024)],
        ["x" * (1536 * 1024)] * 2,
        ["x" * (1536 * 1024)] * 3,
    ],
    ids=["unchanged", "oversized-subcall", "aggregate-below-limit", "aggregate-above-limit"],
)
def test_iteration_detail_survives_operator_retention_and_hydration(payloads):
    step = _iteration_with_details(payloads)
    original = step.model_copy(deep=True)
    hydrated, data = _retain_iteration(step)

    assert step == original
    assert hydrated.iteration == 1
    assert hydrated.duration_ms == 12
    assert hydrated.code == step.code
    assert hydrated.output == step.output
    assert hydrated.usage == step.usage
    assert hydrated.predict_calls[0].signature == "question -> answer"
    assert data["predict_count"] == len(payloads)
    assert [call.usage for call in hydrated.predict_calls[0].calls] == [
        call.usage for call in step.predict_calls[0].calls
    ]
    assert len(json.dumps(data["step"], separators=(",", ":")).encode()) <= (
        agent_module._MAX_EVIDENCE_VALUE_BYTES
    )
    if len(json.dumps(step.model_dump(mode="json"), separators=(",", ":")).encode()) <= (
        agent_module._MAX_EVIDENCE_VALUE_BYTES
    ):
        assert hydrated == step
    else:
        inputs = [call.input for call in hydrated.predict_calls[0].calls]
        assert any(value.get("kind") == "unavailable" for value in inputs)
        assert [call.output for call in hydrated.predict_calls[0].calls] == [
            {"answer": "ready"}
        ] * len(payloads)
        if len(payloads) > 1:
            assert sum(value == {"question": payloads[0]} for value in inputs) == 2


def test_iteration_bounds_tool_payloads_and_text_without_changing_sdk_values():
    from predict_rlm.trace import ToolCall

    step = _iteration_with_details(["small question"])
    huge = "x" * (9 * 1024 * 1024)
    step.reasoning = huge
    step.untruncated_output = huge
    step.tool_calls = [
        ToolCall(
            name="lookup",
            args=[huge],
            kwargs={"query": huge},
            result={"document": huge},
            duration_ms=7,
            error="lookup failed",
        )
    ]
    original = step.model_copy(deep=True)
    hydrated, data = _retain_iteration(step)

    assert step == original
    assert "unavailable" in hydrated.reasoning
    assert "unavailable" in hydrated.untruncated_output
    assert hydrated.code == step.code
    assert hydrated.output == step.output
    tool = hydrated.tool_calls[0]
    assert (tool.name, tool.duration_ms, tool.error) == ("lookup", 7, "lookup failed")
    assert tool.args[0]["kind"] == "unavailable"
    assert tool.kwargs["kind"] == "unavailable"
    assert tool.result["kind"] == "unavailable"
    assert data["tool_count"] == 1


def test_iteration_call_overflow_reports_omitted_counts(monkeypatch):
    from predict_rlm.trace import ToolCall

    monkeypatch.setattr(agent_module, "_MAX_EVIDENCE_VALUE_BYTES", 1200)
    step = _iteration_with_details(["small question"] * 12)
    step.tool_calls = [
        ToolCall(name=f"tool_{index}", duration_ms=index, result=index) for index in range(12)
    ]
    original = step.model_copy(deep=True)
    hydrated, data = _retain_iteration(step)

    assert step == original
    assert hydrated.iteration == step.iteration
    assert hydrated.duration_ms == step.duration_ms
    assert hydrated.usage == step.usage
    assert data["tool_count"] == 12
    assert data["predict_count"] == 12
    omissions = data["omissions"]
    assert "unavailable" in hydrated.reasoning
    assert omissions["tool_count"] + len(hydrated.tool_calls) == 12
    assert (
        omissions["predict_count"] + sum(len(group.calls) for group in hydrated.predict_calls)
        == 12
    )
    assert omissions["tool_count"] > 0
    assert omissions["predict_count"] > 0
    assert len(json.dumps(data["step"], separators=(",", ":")).encode()) <= 1200


def test_iteration_rejects_unrepresentable_metadata_budget_without_fabricating_usage(
    monkeypatch,
):
    from predict_rlm import RunEvent, RunEventKind

    monkeypatch.setattr(agent_module, "_MAX_EVIDENCE_VALUE_BYTES", 128)
    step = _iteration_with_details([])
    step.usage.main.input_tokens = 10**200
    original = step.model_copy(deep=True)
    with pytest.raises(ValueError):
        agent_module._project_evidence_event(
            RunEvent("sdk-run", 1, RunEventKind.ITERATION_RECORDED, 1, {"step": step}),
            invocation_id="invocation",
        )
    assert step == original


_LIVE_OUTPUT_OVERHEAD = len(
    json.dumps({"iteration": 1, "output": ""}, separators=(",", ":")).encode()
)


@pytest.mark.parametrize(
    ("output", "omitted", "error"),
    [
        ("printed output", False, None),
        ("x" * (agent_module._MAX_EVIDENCE_VALUE_BYTES - _LIVE_OUTPUT_OVERHEAD), False, None),
        (
            "x" * (agent_module._MAX_EVIDENCE_VALUE_BYTES - _LIVE_OUTPUT_OVERHEAD + 1),
            True,
            None,
        ),
        ("x" * (9 * 1024 * 1024), True, None),
        ("é" * (agent_module._MAX_EVIDENCE_VALUE_BYTES // 6 + 1), True, None),
        ("x" * (9 * 1024 * 1024), True, "execution failed"),
    ],
    ids=[
        "small",
        "exact-budget",
        "over-budget",
        "large",
        "escaped-bytes",
        "error",
    ],
)
def test_live_execution_output_is_bounded_before_operator_retention(output, omitted, error):
    from predict_rlm import RunEvent, RunEventKind

    from avalanche._agent_trace import AgentTraceEnvelope
    from runtime.operator import Operator
    from runtime.operator.models import NodeState, NodeStatus, RunState, RunStatus

    data = {"iteration": 1, "output": output}
    if error is not None:
        data["error"] = error
    event = RunEvent("sdk-run", 1, RunEventKind.CODE_EXECUTED, 1, data)
    projected = agent_module._project_evidence_event(event, invocation_id="invocation")
    operator = Operator([], watch=False, schedule=False)
    run = RunState(run_id="live-output-run", flow_name="agent-flow", status=RunStatus.RUNNING)
    run.nodes["agent"] = NodeState("agent", "agent", "step", status=NodeStatus.RUNNING)
    operator._runs[run.run_id] = run
    try:
        operator._record_agent_evidence_event(run, "agent", projected)
        snapshot = operator.get_run(run.run_id)
        envelope = AgentTraceEnvelope.model_validate_json(
            snapshot.nodes["agent"].agent_trace_json
        )
        retained = envelope.events[0]
        assert snapshot.status is RunStatus.RUNNING
        assert envelope.trace is None
        assert envelope.evidence is None
        assert retained.event_kind == "code.executed"
        if omitted:
            assert retained.data["output"]["kind"] == "unavailable"
        else:
            assert retained.data["output"] == output
        if error is not None:
            assert retained.data["error"] == error
            assert envelope.error == retained.data["error"]
            assert event.data["error"] == error
        else:
            assert "error" not in retained.data
            assert envelope.error is None
        assert event.data["output"] == output
        assert len(json.dumps(retained.data["output"]).encode()) <= (
            agent_module._MAX_EVIDENCE_VALUE_BYTES
        )
    finally:
        operator.close()


@pytest.mark.parametrize(
    "event_kind",
    ["tool.finished", "predict.finished", "code.executed", "run.failed", "run.cancelled"],
)
@pytest.mark.parametrize(
    "error",
    ["", "x" * 65_536, "x" * 65_537, "é" * 70_000],
    ids=["empty", "exact-limit", "over-limit", "unicode"],
)
def test_all_lifecycle_errors_obey_operator_contract(event_kind, error):
    from predict_rlm import RunEvent, RunEventKind

    data = {
        "call_id": "call",
        "name": "tool",
        "iteration": 1,
        "error_type": "ValueError",
        "error": error,
    }
    event = RunEvent("sdk-run", 1, RunEventKind(event_kind), 1, data)
    projected = agent_module._project_evidence_event(event, invocation_id="invocation")
    retained = _retain_live_event(projected)
    assert retained.event_kind == event_kind
    if len(error) > 65_536:
        assert retained.data["error"].startswith(error[:128])
        assert "unavailable" in retained.data["error"][-128:]
        assert len(retained.data["error"]) <= 65_536
    else:
        assert retained.data["error"] == error
    assert event.data["error"] == error


def _retain_live_event(projected):
    from avalanche._agent_trace import AgentTraceEnvelope
    from runtime.operator import Operator
    from runtime.operator.models import NodeState, NodeStatus, RunState, RunStatus

    operator = Operator([], watch=False, schedule=False)
    run = RunState(run_id="contract-run", flow_name="agent-flow", status=RunStatus.RUNNING)
    run.nodes["agent"] = NodeState("agent", "agent", "step", status=NodeStatus.RUNNING)
    operator._runs[run.run_id] = run
    try:
        operator._record_agent_evidence_event(run, "agent", projected)
        snapshot = operator.get_run(run.run_id)
        assert snapshot.status is RunStatus.RUNNING
        envelope = AgentTraceEnvelope.model_validate_json(
            snapshot.nodes["agent"].agent_trace_json
        )
        return envelope.events[0]
    finally:
        operator.close()


@pytest.mark.parametrize(
    ("event_kind", "data", "omitted_field"),
    [
        ("code.generated", {"iteration": 1, "code": "x" * (9 * 1024 * 1024)}, "code"),
        (
            "predict.started",
            {
                "call_id": "call",
                "model": "test",
                "signature": "x" * (3 * 1024 * 1024),
                "instructions": "y" * (3 * 1024 * 1024),
            },
            "signature",
        ),
        ("run.started", {"inputs": {"question": "x" * (9 * 1024 * 1024)}}, "inputs"),
        (
            "run.succeeded",
            {
                "status": "completed",
                "outputs": {"answer": "x" * (9 * 1024 * 1024)},
            },
            "outputs",
        ),
        ("tool.started", {"call_id": "call", "name": "x" * (9 * 1024 * 1024)}, "name"),
    ],
)
def test_live_inspection_budget_covers_whole_event(event_kind, data, omitted_field):
    from predict_rlm import RunEvent, RunEventKind

    event = RunEvent("sdk-run", 1, RunEventKind(event_kind), 1, data)
    projected = agent_module._project_evidence_event(event, invocation_id="invocation")
    retained = _retain_live_event(projected)
    value = retained.data[omitted_field]
    if isinstance(value, str):
        assert "unavailable" in value
    else:
        assert value["kind"] == "unavailable"
    assert len(json.dumps(retained.data, separators=(",", ":")).encode()) <= (
        agent_module._MAX_EVIDENCE_VALUE_BYTES
    )
    assert "unavailable" not in event.data[omitted_field]


def test_deep_iteration_payload_does_not_reenter_transport_at_terminal():
    payload = {"value": "original"}
    for _ in range(80):
        payload = {"nested": payload}
    step = _iteration_with_details([payload])
    retained, _ = _retain_iteration(step)
    bounded = retained.predict_calls[0].calls[0].input
    assert "unavailable" in json.dumps(bounded)
    assert step.predict_calls[0].calls[0].input == {"question": payload}
