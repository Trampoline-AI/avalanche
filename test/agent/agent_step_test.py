"""Agent invocation, evidence isolation, and failure boundaries without network calls."""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest
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


class Trace:
    def __init__(self, run_id="run", status="completed"):
        self.run_id = run_id
        self.status = status

    def to_exportable_json(self):
        return json.dumps(
            {
                "status": self.status,
                "evidence": {"run_id": self.run_id, "complete": True},
                "steps": [],
            }
        )


@pytest.mark.asyncio
async def test_live_evidence_redacts_tool_and_model_secrets_without_losing_event_order():
    from predict_rlm import RunEvent, RunEventKind

    class Predictor:
        async def acall(self, **inputs):
            sink = agent_module._AvalancheEvidenceSink()
            events = [
                (RunEventKind.RUN_STARTED, {"inputs": inputs}),
                (RunEventKind.PREDICT_STARTED, {"call_id": "p", "input": "model-secret"}),
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
            return SimpleNamespace(note="reviewed", trace=Trace())

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
                    {"call_id": name, "invocation_id": f"caller-{name}"},
                )
            )
            self.arrivals += 1
            if self.arrivals == 2:
                self.ready.set()
            await asyncio.wait_for(self.ready.wait(), timeout=5)
            await sink.close(name, RunEvent(name, 2, RunEventKind.RUN_SUCCEEDED, 2, {}))
            return SimpleNamespace(note=name, trace=Trace(run_id=name))

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
        assert events[0]["data"]["call_id"] == events[-1]["trace"]["evidence"]["run_id"]
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
            await recorder.emit(RunEventKind.RUN_STARTED, inputs=inputs)
            return SimpleNamespace(note="recorded")

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
    if exportable_trace:
        cancellation.trace = Trace(status="cancelled")

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
    if exportable_trace:
        assert observed[0]["kind"] == "trace_finished"
        assert observed[0]["trace"]["status"] == "cancelled"
    else:
        assert observed[0]["kind"] == "trace_unavailable"


@pytest.mark.asyncio
async def test_terminal_persistence_failure_is_not_reclassified_as_agent_failure():
    class Predictor:
        async def acall(self, **inputs):
            return SimpleNamespace(trace=Trace())

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
