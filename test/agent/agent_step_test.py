"""Agent invocation, evidence isolation, and failure boundaries without network calls."""

from __future__ import annotations

import asyncio
import functools
import importlib
import json
import threading
from types import SimpleNamespace
from typing import Literal

import pytest
from dspy import Prediction
from predict_rlm import RunEvidence, RunEvidenceEvent, RunTrace
from pydantic import BaseModel, ValidationError

import avalanche as ava
from avalanche.agent import AgentStepError, AgentStepExecutionError, capture_agent_evidence
from avalanche.runtimes import PredictRLM

predict_rlm_runtime = importlib.import_module("avalanche.runtimes.predict_rlm.runtime")
predict_rlm_events = importlib.import_module("avalanche.runtimes.predict_rlm.events")
predict_rlm_trace = importlib.import_module("avalanche.runtimes.predict_rlm.trace")


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
            return Prediction(
                summary=Summary(headline=f"about {person.name}", person_count=1),
                note=f"review {person.id}",
                trace=_trace(),
                evidence=RunEvidence(
                    run_id="summary-run", complete=True, terminal_outcome="completed"
                ),
            )

    monkeypatch.setattr(
        predict_rlm_runtime, "_build_predictor", lambda *args, **kwargs: Predictor()
    )

    @ava.source
    def load():
        return Person(id=1, name="Ada")

    @ava.agent_step(signature)
    async def summarize(person: Person, *, agent: ava.Agent):
        result = await agent(person=person)
        return {"headline": result.outputs.summary.headline, "note": result.outputs.note}

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

    monkeypatch.setattr(
        predict_rlm_runtime, "_build_predictor", lambda *args, **kwargs: Predictor()
    )
    agent = ava.Agent(signature=SummarySignature, step_name="summarize")
    with pytest.raises(AgentStepError):
        await agent(extra="not in signature")
    assert calls == []
    person = Person(id=1, name="Ada")
    with pytest.raises(AgentStepExecutionError) as raised:
        await agent(person=person)
    assert raised.value.__cause__ is failure
    assert calls == [{"person": person}]


@pytest.mark.asyncio
async def test_agent_rejects_wrongly_typed_inputs_before_invocation(monkeypatch):
    calls = []

    class Predictor:
        async def acall(self, **inputs):
            calls.append(inputs)

    _use_predictor(monkeypatch, Predictor())
    agent = ava.Agent(signature=SummarySignature, step_name="summarize")

    with pytest.raises(AgentStepError, match="person"):
        await agent(person="Ada")
    assert calls == []


@pytest.mark.asyncio
async def test_tools_without_a_name_reach_the_predictor(monkeypatch):
    class Predictor:
        async def acall(self, **inputs):
            return Prediction(
                summary=Summary(headline="h", person_count=1),
                note="n",
                trace=_trace(),
                evidence=RunEvidence(run_id="run", complete=True, terminal_outcome="completed"),
            )

    built = []

    def build(signature, **config):
        built.append(config["tools"])
        return Predictor()

    def lookup(term: str, *, scope: str) -> str:
        return term

    class Search:
        def __call__(self, term: str) -> str:
            return term

    tools = [functools.partial(lookup, scope="all"), Search()]
    monkeypatch.setattr(predict_rlm_runtime, "_build_predictor", build)
    agent = ava.Agent(signature=SummarySignature, step_name="summarize", tools=tools)

    await agent(person=Person(id=1, name="Ada"))

    assert built == [tuple(tools)]


@pytest.mark.asyncio
async def test_invalid_tool_fails_every_call_instead_of_being_dropped(monkeypatch):
    _use_predictor(monkeypatch, object())
    agent = ava.Agent(
        signature=SummarySignature,
        step_name="summarize",
        tools=["not callable"],
    )

    for _ in range(2):
        with pytest.raises(ValidationError):
            await agent(person=Person(id=1, name="Ada"))


def _use_predictor(monkeypatch, predictor):
    monkeypatch.setattr(
        predict_rlm_runtime, "_build_predictor", lambda *args, **kwargs: predictor
    )


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
async def test_live_evidence_redacts_tool_and_model_secrets_without_losing_event_order(
    monkeypatch,
):
    from predict_rlm import RunEvent, RunEventKind

    class Predictor:
        async def acall(self, **inputs):
            sink = predict_rlm_events._AvalancheEvidenceSink()
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
            return Prediction(
                summary=Summary(headline="Ada", person_count=1),
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

    agent = ava.Agent(signature=SummarySignature, step_name="summarize")
    _use_predictor(monkeypatch, Predictor())
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await agent(person=Person(id=1, name="Ada"))

    assert result.outputs.note == "reviewed"
    live = observed[:-1]
    # Avalanche, not the runtime, numbers the live events.
    assert [event.sequence for event in live] == list(range(1, 7))
    assert live[0].native.inputs == {"person": {"id": 1, "name": "Ada"}}
    assert live[-1].native.outputs == {"note": "reviewed"}
    wire = "".join(event.model_dump_json() for event in observed)
    assert "model-secret" not in wire
    assert "tool-secret" not in wire
    terminal = observed[-1]
    assert terminal.kind == "trace_finished"
    assert terminal.complete is True
    assert terminal.record.header.external_run_id == "run"
    assert terminal.record.header.status == "succeeded"
    assert len({event.invocation_id for event in observed}) == 1


@pytest.mark.asyncio
async def test_concurrent_agent_invocations_keep_evidence_and_traces_correlated(monkeypatch):
    from predict_rlm import RunEvent, RunEventKind

    class Predictor:
        def __init__(self):
            self.ready = asyncio.Event()
            self.arrivals = 0

        async def acall(self, **inputs):
            name = inputs["person"].name
            sink = predict_rlm_events._AvalancheEvidenceSink()
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
            return Prediction(
                summary=Summary(headline=name, person_count=1),
                note=name,
                trace=_trace(),
                evidence=RunEvidence(run_id=name, complete=True, terminal_outcome="completed"),
            )

    agent = ava.Agent(signature=SummarySignature, step_name="summarize")
    _use_predictor(monkeypatch, Predictor())
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        left, right = await asyncio.gather(
            agent(person=Person(id=1, name="left")),
            agent(person=Person(id=2, name="right")),
        )
    assert (left.outputs.note, right.outputs.note) == ("left", "right")
    grouped = {}
    for event in observed:
        grouped.setdefault(event.invocation_id, []).append(event)
    assert len(grouped) == 2
    assert not set(grouped) & {"left", "right", "caller-left", "caller-right"}
    for events in grouped.values():
        assert [event.kind for event in events] == ["event", "event", "trace_finished"]
        assert events[0].native.call_id == events[-1].record.header.external_run_id
        assert "caller-" not in events[0].model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["persistence", "interrupt", "cancel"])
async def test_real_recorder_propagates_strict_observer_failures(monkeypatch, failure_kind):
    from predict_rlm import EvidenceIncompleteError, EvidenceRecorder, RunContext, RunEventKind

    predictor = predict_rlm_runtime._build_predictor(SummarySignature, skills=(), tools=())
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
            result = Prediction(
                summary=Summary(headline="Ada", person_count=1), note="recorded", trace=_trace()
            )
            await recorder.finish_success(status="completed", outputs={"note": "recorded"})
            predictor._attach_runtime_evidence(result, recorder)
            return result

    def broken_listener(event):
        if event.kind == "event":
            raise failure

    agent = ava.Agent(signature=SummarySignature, step_name="summarize")
    _use_predictor(monkeypatch, RecorderPredictor())
    expected = AgentStepExecutionError if failure_kind == "persistence" else type(failure)
    with capture_agent_evidence(broken_listener, errors="raise"):
        with pytest.raises(expected) as raised:
            await agent(person=Person(id=1, name="Ada"))
    if failure_kind == "persistence":
        assert isinstance(raised.value.__cause__, EvidenceIncompleteError)
        assert raised.value.__cause__.__cause__ is failure
        with capture_agent_evidence(broken_listener, errors="ignore"):
            assert (await agent(person=Person(id=1, name="Ada"))).outputs.note == "recorded"
    else:
        assert raised.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("exportable_trace", [False, True])
async def test_agent_cancellation_emits_one_terminal_and_preserves_cancellation(
    monkeypatch,
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

    agent = ava.Agent(signature=SummarySignature, step_name="summarize")
    _use_predictor(monkeypatch, Predictor())
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(asyncio.CancelledError) as raised:
            await agent(person=Person(id=1, name="Ada"))
    assert raised.value is cancellation
    assert len(observed) == 1
    (terminal,) = observed
    assert terminal.complete is True
    if exportable_trace:
        assert terminal.kind == "trace_finished"
        assert terminal.record.header.status == "cancelled"
        assert terminal.record.header.external_run_id == "cancelled-run"
    else:
        assert terminal.kind == "trace_unavailable"
        assert (terminal.status, terminal.external_run_id) == ("cancelled", "cancelled-run")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["start", "end"])
async def test_dspy_callback_cancellation_without_sdk_evidence_preserves_cancelled_task(
    monkeypatch, phase
):
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

    agent = ava.Agent(signature=SummarySignature, step_name="summarize")
    _use_predictor(monkeypatch, Predictor(callbacks=[CancelCallback()]))
    task = asyncio.create_task(agent(person=Person(id=1, name="Ada")))
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert raised.value is cancellation
    assert task.cancelled()


@pytest.mark.asyncio
async def test_terminal_persistence_failure_is_not_reclassified_as_agent_failure(monkeypatch):
    class Predictor:
        async def acall(self, **inputs):
            return Prediction(
                trace=_trace(),
                evidence=RunEvidence(
                    run_id="terminal-run", complete=True, terminal_outcome="completed"
                ),
            )

    agent = ava.Agent(signature=SummarySignature, step_name="summarize")
    _use_predictor(monkeypatch, Predictor())
    failure = RuntimeError("terminal persistence failed")
    observed = []

    def persist_then_fail(event):
        observed.append(event)
        raise failure

    with capture_agent_evidence(persist_then_fail, errors="raise"):
        with pytest.raises(RuntimeError) as raised:
            await agent(person=Person(id=1, name="Ada"))
    assert raised.value is failure
    assert [event.kind for event in observed] == ["trace_finished"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exportable_trace", [False, True])
async def test_agent_failure_retains_separate_evidence_without_exposing_raw_events(
    monkeypatch,
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

    agent = ava.Agent(signature=SummarySignature, step_name="summarize")
    _use_predictor(monkeypatch, Predictor())
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AgentStepExecutionError) as raised:
            await agent(person=Person(id=1, name="Ada"))

    assert raised.value.__cause__ is failure
    assert len(observed) == 1
    (terminal,) = observed
    assert terminal.kind == ("trace_finished" if exportable_trace else "trace_unavailable")
    assert terminal.complete is False
    if exportable_trace:
        header = terminal.record.header
        assert (header.status, header.external_run_id) == ("failed", "failed-run")
    else:
        assert (terminal.status, terminal.external_run_id) == ("failed", "failed-run")
    assert "private-model-input" not in terminal.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["trace", "evidence"])
async def test_successful_agent_prediction_requires_sdk_trace_and_evidence(
    monkeypatch, missing_field
):
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

    agent = ava.Agent(signature=SummarySignature, step_name="summarize")
    _use_predictor(monkeypatch, Predictor())
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


def _live_json(event, sequence=1):
    """One PredictRLM event as the operator receives it from an agent step."""
    from avalanche.agent.live import AgentLiveEvent

    native, common = predict_rlm_events.project_event(event)
    return AgentLiveEvent(
        invocation_id="invocation",
        sequence=sequence,
        timestamp_ns=1,
        runtime_kind="predict_rlm",
        common=common,
        native=native,
    ).model_dump_json()


def _retain_iteration(step):
    """Send one recorded turn and its final trace through the operator.

    Returns the turn and PredictRLM's details as the operator serves them.
    """
    from predict_rlm import RunEvent, RunEventKind

    from avalanche._agent_trace import AgentTraceEnvelope
    from avalanche.runtimes.predict_rlm.replay import replay
    from runtime.operator import Operator
    from runtime.operator.models import AgentEventAppended, NodeState, NodeStatus, RunState

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
    # Sub-model calls are ``predict`` tool calls.
    expected_count = len(step.tool_calls) + sum(
        len(group.calls) for group in step.predict_calls
    )
    original_trace = _trace().model_copy(update={"iterations": 1, "steps": [step]})
    sent = replay(
        [RunEvent("sdk-run", 1, RunEventKind.ITERATION_RECORDED, 1, {"step": step})],
        RunEvidence(run_id="sdk-run", complete=True, terminal_outcome="completed"),
        invocation_id="invocation",
        trace=original_trace,
    )
    try:
        for evidence in sent:
            operator._apply_event(
                run.run_id,
                handle,
                {
                    "type": "agent_evidence",
                    "node_id": "agent",
                    "event": evidence.model_dump_json(),
                },
            )
            if evidence.kind == "event":
                while True:
                    update = subscription.get(timeout=5).update
                    if isinstance(update.change, AgentEventAppended):
                        break
                summary = update.change.event
                assert summary.tool_call_count == expected_count
                assert (summary.turn, summary.duration_ms) == (step.iteration, step.duration_ms)
        snapshot = operator.get_latest_run_snapshot(
            run.run_id,
            operator_instance_id=operator.operator_instance_id,
        )
        page = operator.list_agent_events(page_token=snapshot.nodes[0].event_page_token)
        summary = page.events[0]
        assert summary.tool_call_count == expected_count
        envelope = AgentTraceEnvelope.model_validate_json(
            operator.get_run(run.run_id).nodes["agent"].agent_trace_json
        )
        assert envelope.terminal.record.header.status == "succeeded"
        assert original_trace.steps[0] is step
        (event,) = envelope.events
        return event.common, event.native
    finally:
        operator.unsubscribe_operator_updates(subscription)
        operator.close()


def _tools(turn):
    """The turn's tool calls other than its trailing ``predict`` calls."""
    calls = turn.turn.tool_calls
    return calls[: len(calls) - len(turn.turn.native.predict_calls)]


def _predicts(turn):
    calls = turn.turn.tool_calls
    return calls[len(calls) - len(turn.turn.native.predict_calls) :]


def _usage(step):
    from avalanche.runtimes.predict_rlm.events import native_usage

    return native_usage(step.usage)


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
    turn, native = _retain_iteration(step)

    assert step == original
    assert turn.turn.index == 1
    assert turn.turn.duration_ms == 12
    part = turn.turn.native
    assert (part.code, part.output) == (step.code, step.output)
    assert part.usage == _usage(step)
    assert native.omissions is None
    assert [call.signature for call in part.predict_calls] == ["question -> answer"] * len(
        payloads
    )
    assert [call.usage for call in part.predict_calls] == [
        predict_rlm_events.native_token_usage(call.usage)
        for call in step.predict_calls[0].calls
    ]
    # The turn holds the bounded step in its own shape, within the same budget.
    assert len(turn.model_dump_json().encode()) <= (
        predict_rlm_events._MAX_EVIDENCE_VALUE_BYTES + 1024
    )
    inputs = [call.arguments for call in _predicts(turn)]
    if len(json.dumps(step.model_dump(mode="json"), separators=(",", ":")).encode()) <= (
        predict_rlm_events._MAX_EVIDENCE_VALUE_BYTES
    ):
        assert inputs == [{"question": payload} for payload in payloads]
    else:
        assert any(value.get("kind") == "unavailable" for value in inputs)
        assert [call.result for call in _predicts(turn)] == [{"answer": "ready"}] * len(
            payloads
        )
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
    turn, native = _retain_iteration(step)

    assert step == original
    part = turn.turn.native
    assert "unavailable" in turn.turn.message
    assert "unavailable" in part.untruncated_output
    assert (part.code, part.output) == (step.code, step.output)
    (tool,) = _tools(turn)
    assert (tool.name, tool.duration_ms, tool.error) == ("lookup", 7, "lookup failed")
    assert tool.arguments["args"][0]["kind"] == "unavailable"
    assert tool.arguments["kwargs"]["kind"] == "unavailable"
    assert tool.result["kind"] == "unavailable"
    # The tool call and the predict call.
    assert turn.tool_call_count == 2


def test_iteration_call_overflow_reports_omitted_counts(monkeypatch):
    from predict_rlm.trace import ToolCall

    monkeypatch.setattr(predict_rlm_events, "_MAX_EVIDENCE_VALUE_BYTES", 1200)
    step = _iteration_with_details(["small question"] * 12)
    step.tool_calls = [
        ToolCall(name=f"tool_{index}", duration_ms=index, result=index) for index in range(12)
    ]
    original = step.model_copy(deep=True)
    turn, native = _retain_iteration(step)

    assert step == original
    assert turn.turn.index == step.iteration
    assert turn.turn.duration_ms == step.duration_ms
    assert turn.turn.native.usage == _usage(step)
    omissions = native.omissions
    assert "unavailable" in turn.turn.message
    assert omissions.tool_count + len(_tools(turn)) == 12
    assert omissions.predict_count + len(turn.turn.native.predict_calls) == 12
    assert omissions.tool_count > 0
    assert omissions.predict_count > 0
    # The common turn says how many calls it left out, so counts stay true.
    assert turn.tool_call_count == 24


def test_repeated_predict_instructions_stay_within_the_live_turn_budget(monkeypatch):
    from predict_rlm import RunEvent, RunEventKind

    budget = 64 * 1024
    monkeypatch.setattr(predict_rlm_events, "_MAX_EVIDENCE_VALUE_BYTES", budget)
    step = _iteration_with_details(["q"] * 200)
    step.predict_calls[0].instructions = "Answer carefully. " * 200
    _, turn = predict_rlm_events.project_event(
        RunEvent("sdk-run", 1, RunEventKind.ITERATION_RECORDED, 1, {"step": step})
    )

    # Every predict call repeats its group's instructions; the turn is measured that way,
    # leaving only a small fixed cost per call for the turn's own field names.
    assert len(turn.model_dump_json().encode()) <= budget + 200 * 64
    assert all(
        call.instructions != step.predict_calls[0].instructions
        for call in (turn.turn.native.predict_calls)
    )
    assert turn.tool_call_count == 200


def test_iteration_rejects_unrepresentable_metadata_budget_without_fabricating_usage(
    monkeypatch,
):
    from predict_rlm import RunEvent, RunEventKind

    monkeypatch.setattr(predict_rlm_events, "_MAX_EVIDENCE_VALUE_BYTES", 128)
    step = _iteration_with_details([])
    step.usage.main.input_tokens = 10**200
    original = step.model_copy(deep=True)
    with pytest.raises(ValueError):
        predict_rlm_events.project_event(
            RunEvent("sdk-run", 1, RunEventKind.ITERATION_RECORDED, 1, {"step": step})
        )
    assert step == original


_LIVE_OUTPUT_OVERHEAD = len(
    json.dumps({"iteration": 1, "output": ""}, separators=(",", ":")).encode()
)


@pytest.mark.parametrize(
    ("output", "omitted", "error"),
    [
        ("printed output", False, None),
        (
            "x" * (predict_rlm_events._MAX_EVIDENCE_VALUE_BYTES - _LIVE_OUTPUT_OVERHEAD),
            False,
            None,
        ),
        (
            "x" * (predict_rlm_events._MAX_EVIDENCE_VALUE_BYTES - _LIVE_OUTPUT_OVERHEAD + 1),
            True,
            None,
        ),
        ("x" * (9 * 1024 * 1024), True, None),
        ("é" * (predict_rlm_events._MAX_EVIDENCE_VALUE_BYTES // 6 + 1), True, None),
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
    projected = _live_json(event)
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
        retained = envelope.events[0].native
        assert snapshot.status is RunStatus.RUNNING
        assert envelope.terminal is None
        assert retained.kind == "code.executed"
        if omitted:
            assert "unavailable" in retained.output
        else:
            assert retained.output == output
        if error is not None:
            assert retained.execution_error == error
            assert envelope.error == error
            assert event.data["error"] == error
        else:
            assert retained.execution_error is None
            assert envelope.error is None
        assert event.data["output"] == output
        assert len(json.dumps(retained.output).encode()) <= (
            predict_rlm_events._MAX_EVIDENCE_VALUE_BYTES
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
    retained = _retain_live_event(_live_json(event))
    assert retained.native.kind == event_kind
    if len(error) > 65_536:
        assert retained.error.startswith(error[:128])
        assert "unavailable" in retained.error[-128:]
        assert len(retained.error) <= 65_536
    else:
        assert retained.error == error
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
    retained = _retain_live_event(_live_json(event))
    value = {
        "code": lambda: retained.native.code,
        "signature": lambda: retained.native.signature,
        "inputs": lambda: retained.native.inputs,
        "outputs": lambda: retained.native.outputs,
        "name": lambda: retained.common.name,
    }[omitted_field]()
    if isinstance(value, str):
        assert "unavailable" in value
    else:
        assert value["kind"] == "unavailable"
    # Event identity and the common envelope add little to the bounded detail.
    assert len(retained.model_dump_json().encode()) <= (
        predict_rlm_events._MAX_EVIDENCE_VALUE_BYTES + 1024
    )
    assert "unavailable" not in event.data[omitted_field]


def test_deep_iteration_payload_does_not_reenter_transport_at_terminal():
    payload = {"value": "original"}
    for _ in range(80):
        payload = {"nested": payload}
    step = _iteration_with_details([payload])
    turn, _ = _retain_iteration(step)
    (predict,) = _predicts(turn)
    bounded = predict.arguments
    assert "unavailable" in json.dumps(bounded)
    assert step.predict_calls[0].calls[0].input == {"question": payload}


@pytest.mark.asyncio
async def test_codex_lm_is_named_codex_in_declaration_and_run_trace(monkeypatch):
    from dspy_codex_lm import CodexLM

    codex = CodexLM(model="gpt-5.6-terra")

    class Predictor:
        async def acall(self, **inputs):
            return Prediction(
                summary=Summary(headline="about Ada", person_count=1),
                note="reviewed",
                trace=_trace().model_copy(
                    update={"model": codex.model, "sub_model": codex.model}
                ),
                evidence=RunEvidence(
                    run_id="codex-run", complete=True, terminal_outcome="completed"
                ),
            )

    monkeypatch.setattr(
        predict_rlm_runtime, "_build_predictor", lambda *args, **kwargs: Predictor()
    )

    @ava.agent_step(SummarySignature, runtime=PredictRLM(lm=codex, sub_lm=codex))
    async def summarize(person: Person, *, agent: ava.Agent):
        return (await agent(person=person)).outputs.summary

    models = summarize.__agent_step__.declaration().runtime.models
    assert [model.identity["name"] for model in (models.main, models.sub)] == [
        "codex/gpt-5.6-terra",
        "codex/gpt-5.6-terra",
    ]

    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        await summarize.__agent_step__.make_agent()(person=Person(id=1, name="Ada"))
    (finished,) = [event for event in observed if event.kind == "trace_finished"]
    assert (finished.record.header.model, finished.record.native.sub_model) == (
        "codex/gpt-5.6-terra",
        "codex/gpt-5.6-terra",
    )


def test_string_models_keep_their_litellm_ids_in_declaration():
    @ava.agent_step(SummarySignature, runtime=PredictRLM(lm="openai/gpt-5.6-terra"))
    async def summarize(person: Person, *, agent: ava.Agent):
        return (await agent(person=person)).outputs.summary

    models = summarize.__agent_step__.declaration().runtime.models
    assert models.main.identity == "openai/gpt-5.6-terra"
