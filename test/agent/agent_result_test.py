"""Agent calls return validated outputs and the trace, whatever runtime ran them."""

from __future__ import annotations

import importlib
from datetime import date
from pathlib import Path
from typing import ClassVar

import dspy
import pytest
from predict_rlm import File as PredictRLMFile
from predict_rlm import IterationStep, RunEvent, RunEventKind, RunEvidence, RunTrace
from pydantic import BaseModel

import avalanche as ava
from avalanche._agent_evidence import capture_agent_evidence
from avalanche.agent import AgentOutputError, AgentResult
from avalanche.agent.contract import AgentRequest, AgentRunOutcome, JsonOutputs
from avalanche.agent.native.predict_rlm import PredictRLMTokenUsage, PredictRLMUsage
from avalanche.agent.trace import AgentTrace, AgentTraceHeader, PredictRLMNativeTrace
from avalanche.evaluation_capture import capture_evaluations

predict_rlm_runtime = importlib.import_module("avalanche.runtimes.predict_rlm.runtime")
predict_rlm_events = importlib.import_module("avalanche.runtimes.predict_rlm.events")


_NO_USAGE = PredictRLMUsage(
    main=PredictRLMTokenUsage(input_tokens=0, output_tokens=0, cost=0.0, cache_hits=0),
    sub=PredictRLMTokenUsage(input_tokens=0, output_tokens=0, cost=0.0, cache_hits=0),
)


class Count(ava.Signature):
    text: str = ava.InputField()
    count: int = ava.OutputField()


class Deadline(ava.Signature):
    task: str = ava.InputField()
    due: date = ava.OutputField()
    owner: str = ava.OutputField()


def _trace(steps: int = 1) -> RunTrace:
    return RunTrace(
        status="completed",
        model="test-model",
        iterations=steps,
        max_iterations=3,
        duration_ms=5,
        steps=[
            IterationStep(
                iteration=index,
                reasoning="count",
                code="SUBMIT(count=2)",
                output="",
                untruncated_output="",
                duration_ms=5,
            )
            for index in range(1, steps + 1)
        ],
    )


def _use_predictor(monkeypatch, outputs: dict[str, object]) -> None:
    class Predictor:
        async def acall(self, **inputs):
            return dspy.Prediction(
                **outputs,
                trace=_trace(),
                evidence=RunEvidence(run_id="run", complete=True, terminal_outcome="completed"),
            )

    monkeypatch.setattr(predict_rlm_runtime, "_build_predictor", lambda *a, **k: Predictor())


@pytest.mark.asyncio
async def test_agent_returns_validated_outputs_and_the_trace(monkeypatch):
    _use_predictor(monkeypatch, {"count": 2})

    result = await ava.Agent(signature=Count, step_name="count")(text="two words")

    assert isinstance(result, AgentResult)
    assert result.outputs.count == 2
    assert isinstance(result.trace, ava.agent.AgentTrace)
    assert result.trace.header.runtime_kind == "predict_rlm"
    assert result.trace.header.turn_count == len(result.trace.turns) == 1


@pytest.mark.asyncio
async def test_wrong_output_fails_after_the_terminal_trace_and_submits_no_evaluation(
    monkeypatch,
):
    _use_predictor(monkeypatch, {"count": "2"})
    evaluations = ava.Evaluations(
        metrics={
            "quality": ava.Metric(
                state=lambda ctx: ctx.output.count,
                question={"type": "noul", "instructions": "Is the count right?"},
            )
        }
    )

    @ava.agent_step(Count, evaluations=evaluations)
    async def count(text: str, *, agent: ava.Agent) -> int:
        return (await agent(text=text)).outputs.count

    observed, submissions = [], []
    with capture_agent_evidence(observed.append, errors="raise"):
        with capture_evaluations(submissions.append):
            with pytest.raises(AgentOutputError, match="count"):
                await count.fn("two words")

    assert [event.kind for event in observed] == ["trace_finished"]
    assert submissions == []


class _FixedJsonRuntime(BaseModel):
    """A second runtime kind that answers with a fixed JSON document."""

    kind: ClassVar[str] = "fixed_json"

    reply: str

    def merged_over(self, base):
        return self.model_copy()

    def declaration(self, *, workflow_default):
        raise NotImplementedError

    async def invoke(self, request: AgentRequest, events) -> AgentRunOutcome:
        trace = _empty_trace()
        events.finished(trace.model_copy(update={"turns_streamed": True}), complete=True)
        return AgentRunOutcome(outputs=JsonOutputs(text=self.reply), trace=trace)


def _empty_trace() -> AgentTrace:
    return AgentTrace(
        header=AgentTraceHeader(
            runtime_kind="predict_rlm",
            trace_format="predict_rlm.run/v1",
            status="succeeded",
            stop_reason="submit",
            model=None,
            duration_ms=1,
            usage=None,
            turn_count=0,
            tool_call_count=0,
            external_run_id=None,
            session_id=None,
        ),
        turns=(),
        native=PredictRLMNativeTrace(
            sub_model=None, max_iterations=1, usage=_NO_USAGE, telemetry_ref=None
        ),
        turns_streamed=False,
    )


def test_any_runtime_runs_decorated_and_inline_steps_through_json_validation():
    runtime = _FixedJsonRuntime(reply='{"due": "2026-11-02", "owner": "Ada"}')

    @ava.agent_step(Deadline, runtime=runtime)
    async def plan(task: str, *, agent: ava.Agent) -> str:
        outputs = (await agent(task=task)).outputs
        return f"{outputs.owner} by {outputs.due:%d %b}"

    @ava.step
    def combine(summary: str, due: date, owner: str) -> tuple[str, date, str]:
        return summary, due, owner

    @ava.workflow
    def flow():
        deadline = ava.agent.step(Deadline, inputs={"task": "ship"}, runtime=runtime)
        return combine(plan("ship"), deadline[0], deadline[1])

    assert flow().run(executor=ava.LocalExecutor()).result() == (
        "Ada by 02 Nov",
        date(2026, 11, 2),
        "Ada",
    )


@pytest.mark.asyncio
async def test_json_outputs_are_validated_strictly():
    agent = ava.Agent(
        signature=Deadline,
        step_name="plan",
        runtime=_FixedJsonRuntime(reply='{"due": "2026-11-02", "owner": 7}'),
    )

    with pytest.raises(AgentOutputError, match="owner"):
        await agent(task="ship")


class Redact(ava.Signature):
    """Redact the document."""

    document: ava.File = ava.InputField()
    attachments: list[ava.File] = ava.InputField()
    redacted: ava.File = ava.OutputField()
    notes: str = ava.OutputField()


@pytest.mark.asyncio
async def test_predict_rlm_mounts_ava_files_and_returns_reserved_outputs(monkeypatch, tmp_path):
    source = tmp_path / "doc.txt"
    source.write_bytes(b"secret")
    attachment = tmp_path / "a.txt"
    attachment.write_bytes(b"a")
    workspace = ava.Workspace.from_directory(tmp_path, reference="snapshot:test")
    received: dict[str, object] = {}
    built = []

    def build(signature, **config):
        built.append(signature)
        output_dir = Path(config["output_dir"])

        class Predictor:
            async def acall(self, **inputs):
                await predict_rlm_events._AvalancheEvidenceSink().emit(
                    RunEvent("run", 1, RunEventKind.RUN_STARTED, 1, {"inputs": inputs})
                )
                received["document"] = inputs["document"].path
                received["attachments"] = [file.path for file in inputs["attachments"]]
                produced = output_dir / "redacted" / "redacted.txt"
                produced.parent.mkdir()
                produced.write_bytes(b"[redacted]")
                return dspy.Prediction(
                    redacted=PredictRLMFile(path=str(produced)),
                    notes="done",
                    trace=_trace(),
                    evidence=RunEvidence(
                        run_id="run", complete=True, terminal_outcome="completed"
                    ),
                )

        return Predictor()

    monkeypatch.setattr(predict_rlm_runtime, "_build_predictor", build)

    observed = []
    with (
        capture_agent_evidence(observed.append, errors="raise"),
        ava.file_reference_context(workspace, attempt="attempt-1"),
    ):
        result = await ava.Agent(signature=Redact, step_name="redact")(
            document=ava.File.from_path(source, workspace=workspace),
            attachments=[ava.File.from_path(attachment, workspace=workspace)],
        )

    # Inputs are mounted where they are, never copied.
    assert received == {"document": str(source), "attachments": [str(attachment)]}
    # Evidence describes the caller's file reference, never the mounted host path.
    assert observed[0].native.inputs["document"]["workspace"] == "snapshot:test"
    assert observed[0].native.inputs["document"]["path"] == "doc.txt"
    redacted = result.outputs.redacted
    assert isinstance(redacted, ava.File)
    assert redacted.read_bytes() == b"[redacted]"
    assert redacted.ref.workspace == "snapshot:test"
    assert redacted.ref.path.startswith(".outputs/redact/attempt-1/")
    assert result.outputs.notes == "done"
    assert built[0].input_fields["document"].annotation is PredictRLMFile
    assert built[0].input_fields["attachments"].annotation == list[PredictRLMFile]
    assert built[0].output_fields["redacted"].annotation is PredictRLMFile


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redacted",
    [
        None,
        "not a file",
        PredictRLMFile(path=None),
        PredictRLMFile(path="/missing/file"),
        PredictRLMFile(path=__file__),
    ],
    ids=["missing-value", "wrong-type", "no-path", "unreadable", "outside-reservation"],
)
async def test_predict_rlm_file_outputs_that_cannot_be_used_are_output_errors(
    monkeypatch, tmp_path, redacted
):
    _use_predictor(monkeypatch, {"redacted": redacted, "notes": "done"})
    source = tmp_path / "doc.txt"
    source.write_bytes(b"secret")

    with pytest.raises(AgentOutputError, match="redacted"):
        await ava.Agent(signature=Redact, step_name="redact")(
            document=ava.File.from_path(source), attachments=[]
        )


class NestedFile(ava.Signature):
    documents: dict[str, ava.File] = ava.InputField()
    answer: str = ava.OutputField()


@pytest.mark.asyncio
async def test_predict_rlm_mounts_files_nested_inside_other_types(monkeypatch, tmp_path):
    source = tmp_path / "a.txt"
    source.write_bytes(b"a")
    received = {}

    class Predictor:
        async def acall(self, **inputs):
            received.update(inputs)
            return dspy.Prediction(
                answer="x",
                trace=_trace(),
                evidence=RunEvidence(run_id="run", complete=True, terminal_outcome="completed"),
            )

    monkeypatch.setattr(predict_rlm_runtime, "_build_predictor", lambda *_, **__: Predictor())

    result = await ava.Agent(signature=NestedFile, step_name="nested")(
        documents={"a": ava.File.from_path(source)}
    )

    assert isinstance(received["documents"]["a"], PredictRLMFile)
    assert received["documents"]["a"].path == str(source)
    assert result.outputs.answer == "x"
