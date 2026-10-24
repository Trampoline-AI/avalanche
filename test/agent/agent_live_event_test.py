"""Live agent events and final traces as they cross from a step to the operator."""

from __future__ import annotations

import pytest
from predict_rlm import IterationStep, RunEvent, RunEventKind, RunEvidence, RunTrace
from pydantic import ValidationError

import avalanche as ava
from avalanche._agent_evidence import capture_agent_evidence
from avalanche.agent import AgentStepError
from avalanche.agent.live import (
    AGENT_EVIDENCE,
    AgentLiveEvent,
    AgentTraceFinished,
    CodeGenerated,
    ToolStarted,
)
from avalanche.agent.native.predict_rlm import Correlated, RunStarted
from avalanche.agent.steps import _InvocationEvents
from avalanche.runtimes.predict_rlm.replay import replay


def _step() -> IterationStep:
    return IterationStep(
        iteration=1,
        reasoning="look",
        code="print(1)",
        output="1",
        untruncated_output="1",
        duration_ms=3,
    )


def _sent():
    events = [
        RunEvent("run", 1, RunEventKind.RUN_STARTED, 1, {"inputs": {"q": "x"}}),
        RunEvent(
            "run", 2, RunEventKind.CODE_GENERATED, 2, {"iteration": 1, "code": "print(1)"}
        ),
        RunEvent("run", 3, RunEventKind.CODE_EXECUTED, 3, {"iteration": 1, "output": "1"}),
        RunEvent("run", 4, RunEventKind.ITERATION_RECORDED, 4, {"step": _step()}),
        RunEvent(
            "run", 5, RunEventKind.RUN_SUCCEEDED, 5, {"status": "completed", "outputs": {}}
        ),
    ]
    trace = RunTrace(
        status="completed",
        model="m",
        iterations=1,
        max_iterations=2,
        duration_ms=4,
        steps=[_step()],
    )
    return replay(
        events,
        RunEvidence(run_id="run", complete=True, terminal_outcome="completed"),
        invocation_id="invocation",
        trace=trace,
    )


def test_every_body_round_trips_through_json_and_turns_stream_live():
    sent = _sent()

    restored = [
        AGENT_EVIDENCE.validate_json(body.model_dump_json(), strict=True) for body in sent
    ]

    assert restored == sent
    assert [body.sequence for body in sent[:-1]] == [1, 2, 3, 4, 5]
    final = sent[-1]
    assert isinstance(final, AgentTraceFinished)
    assert final.record.turns == ()
    assert final.record.header.turn_count == 1


def _live(**changes) -> AgentLiveEvent:
    fields = {
        "invocation_id": "invocation",
        "sequence": 1,
        "timestamp_ns": 1,
        "runtime_kind": "predict_rlm",
        "common": CodeGenerated(turn=1, code="x"),
        "native": Correlated(kind="code.generated"),
    }
    fields.update(changes)
    return AgentLiveEvent.model_validate(fields)


@pytest.mark.parametrize(
    "changes",
    [
        {"common": None},
        {"common": ToolStarted(call_id="t", name="lookup")},
        {"native": RunStarted(input_fields=(), inputs={})},
        {"schema_version": 1},
    ],
    ids=["missing-common", "other-common", "native-without-common", "v1"],
)
def test_a_native_event_must_travel_with_its_common_event(changes):
    with pytest.raises(ValidationError):
        _live(**changes)


def test_a_final_trace_on_the_wire_has_streamed_its_turns():
    final = _sent()[-1]
    unstreamed = final.record.model_copy(update={"turns_streamed": False})

    with pytest.raises(ValidationError, match="streamed"):
        AgentTraceFinished(invocation_id="invocation", record=unstreamed, complete=True)


def test_an_invocation_sends_one_final_trace_and_nothing_after_it():
    final = _sent()[-1]
    events = _InvocationEvents("invocation", "predict_rlm")
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        events.finished(final.record, complete=True)
        with pytest.raises(AgentStepError, match="second final trace"):
            events.finished(final.record, complete=True)
        with pytest.raises(AgentStepError, match="after its final trace"):
            events.record(Correlated(kind="code.generated"), CodeGenerated(turn=1, code="x"))
    assert [body.kind for body in observed] == ["trace_finished"]


class Echo(ava.Signature):
    text: str = ava.InputField()
    answer: str = ava.OutputField()


class _SilentRuntime:
    """A runtime that returns without its final trace."""

    kind = "predict_rlm"

    def merged_over(self, base):
        return self

    def declaration(self, *, workflow_default):
        raise NotImplementedError

    async def invoke(self, request, events):
        from avalanche.agent.contract import AgentRunOutcome, JsonOutputs

        return AgentRunOutcome(
            outputs=JsonOutputs(text='{"answer": "a"}'), trace=_sent()[-1].record
        )


@pytest.mark.asyncio
async def test_a_runtime_that_skips_its_final_trace_fails_the_call():
    agent = ava.Agent(signature=Echo, step_name="echo", runtime=_SilentRuntime())

    with pytest.raises(AgentStepError, match="final trace"):
        await agent(text="hi")


def test_a_handled_tool_failure_does_not_fail_the_turn():
    from predict_rlm.trace import ToolCall

    from avalanche.runtimes.predict_rlm.events import project_event

    step = _step()
    step.tool_calls = [ToolCall(name="lookup", duration_ms=1, error="not found")]
    native, common = project_event(
        RunEvent("run", 1, RunEventKind.ITERATION_RECORDED, 1, {"step": step})
    )
    event = _live(common=common, native=native)

    assert event.failed is False
    failed_code = step.model_copy(update={"error": True})
    native, common = project_event(
        RunEvent("run", 1, RunEventKind.ITERATION_RECORDED, 1, {"step": failed_code})
    )
    assert _live(common=common, native=native).failed is True
