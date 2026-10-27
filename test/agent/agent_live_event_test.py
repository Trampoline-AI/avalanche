"""Live agent events and final traces as they cross from a step to the operator."""

from __future__ import annotations

from datetime import datetime

import pytest
from predict_rlm import IterationStep, RunEvent, RunEventKind, RunEvidence, RunTrace
from pydantic import ValidationError

import avalanche as ava
from avalanche._agent_evidence import capture_agent_evidence
from avalanche.agent import AgentStepError
from avalanche.agent.contract import PythonOutputs
from avalanche.agent.live import (
    AGENT_EVIDENCE,
    AgentLiveEvent,
    AgentTraceFinished,
    RunStarted,
    ToolStarted,
)
from avalanche.agent.native.claude_agent import SessionInitialized
from avalanche.agent.native.predict_rlm import CodeGenerated
from avalanche.agent.steps import _InvocationEvents
from avalanche.agent.trace import RETAINED_VALUES_LIMIT_BYTES
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
            "run",
            5,
            RunEventKind.RUN_SUCCEEDED,
            5,
            {"status": "completed", "outputs": {"answer": "yes"}},
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
    # A run's success is its final event, so it sends no live event of its own.
    assert [body.sequence for body in sent[:-1]] == [1, 2, 3, 4]
    started, *_, turn = sent[:-1]
    assert started.native is None
    assert started.common == RunStarted(model=None, inputs={"q": "x"}, omitted_inputs=())
    # A turn its live event kept whole needs no native part.
    assert turn.native is None and turn.common.kind == "turn.finished"
    final = sent[-1]
    assert isinstance(final, AgentTraceFinished)
    assert final.header.turn_count == 1
    assert final.outputs == {"answer": "yes"}


def _live(**changes) -> AgentLiveEvent:
    fields = {
        "invocation_id": "invocation",
        "sequence": 1,
        "timestamp_ns": 1,
        "common": ToolStarted(call_id="t", name="lookup", turn=1),
        "native": None,
    }
    fields.update(changes)
    return AgentLiveEvent.model_validate(fields)


@pytest.mark.parametrize(
    ("changes", "runtime_kind"),
    [
        ({}, None),
        ({"common": None, "native": CodeGenerated(iteration=1, code="x")}, "predict_rlm"),
        (
            {"native": SessionInitialized(session_id="s", model=None, tools=())},
            "claude_agent",
        ),
    ],
    ids=["common-only", "native-only", "both"],
)
def test_a_live_event_has_a_common_part_a_native_part_or_both(changes, runtime_kind):
    event = _live(**changes)

    # Only a native part names the runtime that sent the event.
    assert event.runtime_kind == runtime_kind
    assert "runtime_kind" not in event.model_dump()


@pytest.mark.parametrize(
    "changes",
    [{"common": None}, {"schema_version": 1}, {"runtime_kind": "predict_rlm"}],
    ids=["empty", "v1", "stored-runtime-kind"],
)
def test_a_live_event_rejects_an_invalid_body(changes):
    with pytest.raises(ValidationError):
        _live(**changes)


def test_the_final_event_keeps_outputs_within_the_size_limit():
    header = _sent()[-1].header
    events = _InvocationEvents("invocation", "predict_rlm")
    observed = []
    big = "x" * (RETAINED_VALUES_LIMIT_BYTES + 1)
    with capture_agent_evidence(observed.append, errors="raise"):
        events.finished(
            header, outputs=PythonOutputs(values={"small": 1, "big": big}), complete=True
        )

    (final,) = observed
    assert final.outputs == {"small": 1}
    assert final.omitted_outputs == ("big",)


@pytest.mark.parametrize(
    "changes",
    [
        {"outputs": None, "omitted_outputs": ("answer",)},
        {"outputs": {"answer": "a"}, "omitted_outputs": ("answer",)},
    ],
    ids=["omitted-without-outputs", "kept-and-omitted"],
)
def test_a_final_event_rejects_contradictory_outputs(changes):
    final = _sent()[-1]

    with pytest.raises(ValidationError):
        AgentTraceFinished.model_validate({**final.model_dump(), **changes})


def test_an_invocation_sends_one_final_trace_and_nothing_after_it():
    final = _sent()[-1]
    events = _InvocationEvents("invocation", "predict_rlm")
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        events.finished(final.header, outputs=None, complete=True)
        with pytest.raises(AgentStepError, match="second final trace"):
            events.finished(final.header, outputs=None, complete=True)
        with pytest.raises(AgentStepError, match="after its final trace"):
            events.record(CodeGenerated(iteration=1, code="x"), None)
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
        from avalanche.runtimes.predict_rlm.trace import trace_record

        trace = RunTrace(
            status="completed", model="m", iterations=0, max_iterations=2, duration_ms=1
        )
        evidence = RunEvidence(run_id="run", complete=True, terminal_outcome="completed")
        return AgentRunOutcome(
            outputs=JsonOutputs(text='{"answer": "a"}'),
            trace=trace_record(trace, evidence, model=None, sub_model=None),
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


def test_a_turn_travels_with_a_native_event_of_its_own_format():
    from avalanche.runtimes.predict_rlm.events import project_event

    _, common = project_event(
        RunEvent("run", 1, RunEventKind.ITERATION_RECORDED, 1, {"step": _step()})
    )

    with pytest.raises(ValidationError, match="does not belong"):
        _live(common=common, native=SessionInitialized(session_id="s", model=None, tools=()))


def test_a_file_output_travels_as_its_file_reference_never_its_host_path(tmp_path):
    from predict_rlm import File as PredictFile

    from avalanche.runtimes.predict_rlm.files import AgentFileBridge, predict_signature

    class Redact(ava.Signature):
        document: str = ava.InputField()
        redacted: ava.File = ava.OutputField()
        reviewed_at: datetime = ava.OutputField()

    header = _sent()[-1].header
    workspace = ava.Workspace.from_directory(tmp_path, reference="snapshot:test")
    observed = []
    with ava.file_reference_context(workspace, attempt="attempt-1"):
        bridge = AgentFileBridge("redact", predict_signature(Redact))
        output = bridge.output_dir / "redacted.txt"
        output.write_bytes(b"[redacted]")
        events = _InvocationEvents("invocation", "predict_rlm")
        with capture_agent_evidence(observed.append, errors="raise"):
            events.finished(
                header,
                outputs=PythonOutputs(
                    values={
                        "redacted": bridge.outputs(PredictFile(path=str(output))),
                        "reviewed_at": "2026-10-27T09:00:00",
                    }
                ),
                complete=True,
            )

    (final,) = observed
    assert final.outputs["reviewed_at"] == "2026-10-27T09:00:00"
    redacted = final.outputs["redacted"]
    assert redacted["kind"] == "file"
    assert redacted["workspace"] == "snapshot:test"
    assert redacted["path"].startswith(".outputs/redact/attempt-1/")
    assert str(tmp_path) not in final.model_dump_json()


def test_typed_values_keep_their_json_form_and_host_paths_never_appear(tmp_path):
    import uuid
    from datetime import date
    from decimal import Decimal

    header = _sent()[-1].header
    events = _InvocationEvents("invocation", "predict_rlm")
    observed = []
    key = uuid.UUID(int=1)
    with capture_agent_evidence(observed.append, errors="raise"):
        events.finished(
            header,
            outputs=PythonOutputs(
                values={
                    "due": date(2026, 10, 27),
                    "key": key,
                    "amount": Decimal("1.5"),
                    "where": tmp_path,
                }
            ),
            complete=True,
        )

    (final,) = observed
    assert final.outputs["due"] == "2026-10-27"
    assert final.outputs["key"] == str(key)
    assert final.outputs["amount"] == "1.5"
    assert final.outputs["where"]["kind"] == "unavailable"
    assert str(tmp_path) not in final.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["not json", "[1]"], ids=["not-json", "not-an-object"])
async def test_json_outputs_that_are_not_an_object_still_send_the_final_event(reply):
    from avalanche.agent import AgentOutputError
    from avalanche.agent.contract import AgentRunOutcome, JsonOutputs
    from avalanche.runtimes.predict_rlm.trace import trace_record

    class _Runtime(_SilentRuntime):
        async def invoke(self, request, events):
            trace = trace_record(
                RunTrace(
                    status="completed", model="m", iterations=0, max_iterations=2, duration_ms=1
                ),
                RunEvidence(run_id="run", complete=True, terminal_outcome="completed"),
                model=None,
                sub_model=None,
            )
            outputs = JsonOutputs(text=reply)
            events.finished(trace.header, outputs=outputs, complete=True)
            return AgentRunOutcome(outputs=outputs, trace=trace)

    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AgentOutputError):
            await ava.Agent(signature=Echo, step_name="echo", runtime=_Runtime())(text="hi")

    (final,) = observed
    assert (final.kind, final.outputs) == ("trace_finished", None)
