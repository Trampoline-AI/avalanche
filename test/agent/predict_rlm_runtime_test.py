"""The PredictRLM runtime: its descriptor and its mapping onto the common trace."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import pickle
import subprocess
import sys
import textwrap

import predict_rlm
import pytest
from dspy import Prediction
from predict_rlm import IterationStep, RunEvent, RunEventKind, RunEvidence, RunTrace
from predict_rlm.trace import (
    LMUsage,
    PredictCallDetail,
    PredictCallGroup,
    TokenUsage,
    ToolCall,
)

import avalanche as ava
from avalanche.agent.contract import (
    AgentRequest,
    RunStarted,
    SignatureSpec,
    ToolStarted,
    TurnFinished,
)
from avalanche.agent.native.predict_rlm import PredictRLMPredictCall, PredictRLMTokenUsage
from avalanche.agent.trace import ToolCall as CommonToolCall
from avalanche.runtimes import PredictRLM

runtime_module = importlib.import_module("avalanche.runtimes.predict_rlm.runtime")
events_module = importlib.import_module("avalanche.runtimes.predict_rlm.events")


class Answer(ava.Signature):
    question: str = ava.InputField()
    answer: str = ava.OutputField()


def _request(spec: SignatureSpec | None = None) -> AgentRequest:
    spec = spec or SignatureSpec.from_dspy(Answer)
    return AgentRequest(
        invocation_id="invocation",
        step_name="answer",
        signature=spec,
        inputs=spec.input_model(question="q"),
        tools=(),
    )


def _step() -> IterationStep:
    return IterationStep(
        iteration=1,
        reasoning="look it up",
        code="lookup('q')",
        output="found",
        untruncated_output="found",
        duration_ms=7,
        tool_calls=[ToolCall(name="lookup", args=["q"], result="a", duration_ms=2)],
        predict_calls=[
            PredictCallGroup(
                signature="text -> summary",
                model="sub-model",
                total_usage=TokenUsage(input_tokens=3, output_tokens=1),
                calls=[
                    PredictCallDetail(
                        duration_ms=4,
                        usage=TokenUsage(input_tokens=3, output_tokens=1),
                        input={"text": "q"},
                        output={"summary": "s"},
                    )
                ],
            )
        ],
        usage=LMUsage(
            main=TokenUsage(input_tokens=10, output_tokens=5, cost=0.25, cache_hits=1),
            sub=TokenUsage(input_tokens=3, output_tokens=1, cost=0.05),
        ),
    )


def _prediction(status: str = "completed", outcome: str = "completed") -> Prediction:
    step = _step()
    return Prediction(
        answer="a",
        trace=RunTrace(
            status=status,
            model="main-model",
            sub_model="sub-model",
            iterations=1,
            max_iterations=4,
            duration_ms=20,
            usage=step.usage,
            steps=[step],
        ),
        evidence=RunEvidence(run_id="sdk-run", complete=True, terminal_outcome=outcome),
    )


class _Recorder:
    """An invocation sink that keeps what the runtime sent."""

    def __init__(self) -> None:
        self.events: list[tuple[object, object]] = []
        self.terminals: list[object] = []

    async def emit(self, native: object, common: object) -> None:
        self.events.append((native, common))

    def finished(self, header: object, *, outputs: object, complete: bool) -> None:
        self.terminals.append((header, outputs))

    def unavailable(self, **detail: object) -> None:
        self.terminals.append(detail)


def _use_predictor(monkeypatch, predictor) -> list[dict[str, object]]:
    builds: list[dict[str, object]] = []

    def build(signature, **config):
        builds.append(config)
        return predictor

    monkeypatch.setattr(runtime_module, "_build_predictor", build)
    return builds


def test_descriptor_fields_mirror_the_predict_rlm_constructor():
    constructor = inspect.signature(predict_rlm.PredictRLM.__init__).parameters
    mirrored = {
        name: parameter.default
        for name, parameter in constructor.items()
        if name not in {"self", "signature", "tools"}
    }

    # Avalanche's one deliberate default: agent steps run quietly.
    mirrored["verbose"] = False
    assert {name: field.default for name, field in PredictRLM.model_fields.items()} == mirrored


def test_runtime_imports_without_the_optional_sandbox_extra():
    # ``websockets`` ships only with predict-rlm's optional ``sbx`` extra.
    script = textwrap.dedent(
        """
        import importlib.abc, sys

        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name.split(".")[0] == "websockets":
                    raise ImportError(name)

        sys.meta_path.insert(0, Block())
        from avalanche.runtimes import PredictRLM

        PredictRLM(lm="openai/gpt-5.5")
        """
    )

    subprocess.run([sys.executable, "-c", script], check=True)


@pytest.mark.asyncio
async def test_other_options_pass_unchanged_to_the_predict_rlm_constructor(monkeypatch):
    received: list[dict[str, object]] = []

    class Constructor:
        def __init__(self, signature, **options):
            received.append(options)

        async def acall(self, **inputs):
            return _prediction()

    monkeypatch.setattr(predict_rlm, "PredictRLM", Constructor)
    runtime = pickle.loads(pickle.dumps(PredictRLM(max_iterations=4, future_option=("a", 2))))

    await runtime.invoke(_request(), _Recorder())

    assert received[0]["max_iterations"] == 4
    assert received[0]["future_option"] == ("a", 2)


@pytest.mark.asyncio
async def test_an_unknown_option_fails_on_first_run_with_predict_rlm_error():
    runtime = PredictRLM(max_turns=3)

    with pytest.raises(TypeError, match="max_turns"):
        await runtime.invoke(_request(), _Recorder())


@pytest.mark.asyncio
async def test_invoke_maps_a_run_onto_the_common_trace(monkeypatch):
    class Predictor:
        async def acall(self, **inputs):
            return _prediction()

    _use_predictor(monkeypatch, Predictor())

    outcome = await PredictRLM(lm="main-model").invoke(_request(), _Recorder())

    assert dict(outcome.outputs.values) == {"answer": "a"}
    header = outcome.trace.header
    assert (header.runtime_kind, header.status, header.stop_reason) == (
        "predict_rlm",
        "succeeded",
        "submit",
    )
    # The sub-model call counts as a tool call.
    assert (header.turn_count, header.tool_call_count, header.external_run_id) == (
        1,
        2,
        "sdk-run",
    )
    assert (header.usage.input_tokens, header.usage.output_tokens) == (13, 6)
    assert header.usage.cost_usd == pytest.approx(0.3)
    native = outcome.trace.header.native
    assert (native.sub_model, native.max_iterations) == ("sub-model", 4)
    assert native.usage.main.cache_hits + native.usage.sub.cache_hits == 1
    (turn,) = outcome.trace.turns
    assert [call.name for call in turn.tool_calls] == ["lookup", "predict"]
    assert turn.tool_calls[-1] == CommonToolCall(
        call_id=None,
        caller="code",
        name="predict",
        arguments={"text": "q"},
        result={"summary": "s"},
        error=None,
        duration_ms=4,
    )
    assert turn.native.predict_calls == (
        PredictRLMPredictCall(
            signature="text -> summary",
            instructions=None,
            model="sub-model",
            usage=PredictRLMTokenUsage(input_tokens=3, output_tokens=1, cost=0.0, cache_hits=0),
            finish_reason=None,
        ),
    )
    assert (turn.native.code, turn.native.output, turn.native.error) == (
        "lookup('q')",
        "found",
        False,
    )
    # What PredictRLM writes each iteration is the turn's message, not provider reasoning.
    assert (turn.message, turn.reasoning) == ("look it up", None)


@pytest.mark.parametrize(
    ("status", "outcome", "expected"),
    [
        ("max_iterations", "completed", ("succeeded", "max_iterations")),
        ("error", "error", ("failed", None)),
        ("in_progress", "cancelled", ("cancelled", None)),
    ],
)
def test_trace_status_follows_the_run_outcome(status, outcome, expected):
    from avalanche.runtimes.predict_rlm.trace import trace_record

    prediction = _prediction(status, outcome)

    record = trace_record(prediction.trace, prediction.evidence, model=None, sub_model=None)

    assert (record.header.status, record.header.stop_reason) == expected


@pytest.mark.asyncio
async def test_live_predict_rlm_events_reach_the_runtime_sink_as_common_events(monkeypatch):
    class Predictor:
        async def acall(self, **inputs):
            sink = events_module._AvalancheEvidenceSink()
            events = [
                (RunEventKind.RUN_STARTED, {"inputs": inputs}),
                (RunEventKind.CODE_GENERATED, {"iteration": 1, "code": "lookup('q')"}),
                (RunEventKind.TOOL_STARTED, {"call_id": "t", "name": "lookup"}),
                (
                    RunEventKind.PREDICT_STARTED,
                    {
                        "call_id": "p",
                        "signature": "text -> summary",
                        "instructions": None,
                        "model": "sub-model",
                    },
                ),
                (
                    RunEventKind.ITERATION_RECORDED,
                    {"step": _step().model_dump(mode="json")},
                ),
            ]
            for sequence, (kind, data) in enumerate(events, 1):
                await sink.emit(RunEvent("run", sequence, kind, sequence, data))
            return _prediction()

    _use_predictor(monkeypatch, Predictor())
    recorder = _Recorder()

    await PredictRLM().invoke(_request(), recorder)

    assert [
        (native.kind if native is not None else None, type(common))
        for native, common in recorder.events
    ] == [
        (None, RunStarted),
        ("code.generated", type(None)),
        (None, ToolStarted),
        ("predict.started", type(None)),
        (None, TurnFinished),
    ]
    native, finished = recorder.events[-1]
    assert finished.turn.index == 1
    # The turn carries PredictRLM's own details; a native event only what it left out.
    assert finished.turn.native.untruncated_output == _step().untruncated_output
    assert native is None
    # The final event carries the header and the outputs, never the turns again.
    [(header, outputs)] = recorder.terminals
    assert header.turn_count == 1
    assert dict(outputs.values) == {"answer": "a"}


@pytest.mark.asyncio
async def test_a_base_exception_from_the_runtime_sink_propagates_unchanged(monkeypatch):
    class Stop(BaseException):
        pass

    class StoppingSink(_Recorder):
        async def emit(self, native: object, common: object) -> None:
            raise Stop

    class Predictor:
        async def acall(self, **inputs):
            sink = events_module._AvalancheEvidenceSink()
            try:
                await sink.emit(
                    RunEvent(
                        "run",
                        1,
                        RunEventKind.CODE_GENERATED,
                        1,
                        {"iteration": 1, "code": "x"},
                    )
                )
            except BaseException as exc:
                # A strict PredictRLM sink failure surfaces as an ordinary error.
                error = RuntimeError("strict evidence sink failed")
                error.evidence = RunEvidence(
                    run_id="run", complete=False, terminal_outcome="error"
                )
                raise error from exc
            return _prediction()

    _use_predictor(monkeypatch, Predictor())

    with pytest.raises(Stop):
        await PredictRLM().invoke(_request(), StoppingSink())


@pytest.mark.asyncio
async def test_predictor_is_built_once_per_request_contract(monkeypatch):
    class Predictor:
        async def acall(self, **inputs):
            return _prediction()

    builds = _use_predictor(monkeypatch, Predictor())
    runtime = PredictRLM(max_iterations=4)
    spec = SignatureSpec.from_dspy(Answer)

    await runtime.invoke(_request(spec), _Recorder())
    await runtime.invoke(_request(spec), _Recorder())
    await runtime.invoke(_request(), _Recorder())

    assert builds == [{"skills": (), "tools": (), "max_iterations": 4, "verbose": False}] * 2


def test_file_and_workspace_inputs_are_recorded_without_bytes(tmp_path):
    source = tmp_path / "a.txt"
    source.write_bytes(b"secret bytes")
    document = ava.File.from_path(source)
    event = RunEvent(
        "run",
        1,
        RunEventKind.RUN_STARTED,
        1,
        {"inputs": {"document": document, "workspace": ava.Workspace()}},
    )

    native, common = events_module.project_event(event)

    assert native is None
    assert common.inputs == {
        "document": {
            "kind": "file",
            "workspace": document.ref.workspace,
            "path": "a.txt",
            "name": "a.txt",
            "content_type": None,
            "size_bytes": 12,
            "sha256": hashlib.sha256(b"secret bytes").hexdigest(),
        },
        "workspace": {"kind": "workspace", "entry_count": 0, "total_size_bytes": 0},
    }


def test_every_predict_rlm_event_kind_has_a_native_or_common_form():
    from typing import get_args

    from avalanche.agent.live import AgentEvent
    from avalanche.agent.native import predict_rlm as native

    def kinds(union):
        members = get_args(get_args(union)[0])
        return {
            kind
            for member in members
            for kind in get_args(member.model_fields["kind"].annotation)
        }

    # A run's success is the final event, not a live one.
    modelled = kinds(native.PredictRLMNativeEvent) | kinds(AgentEvent) | {"run.succeeded"}
    assert modelled >= {kind.value for kind in RunEventKind}


@pytest.mark.parametrize("kind", ["session.started", "input.prepared", "session.released"])
def test_sandbox_lifecycle_events_reach_the_sink_without_a_common_event(kind):
    native, common = events_module.project_event(
        RunEvent("run", 1, RunEventKind(kind), 1, {"field": "question"})
    )

    assert (native.kind, common) == (kind, None)
