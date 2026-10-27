"""Behavior every agent runtime must provide, checked against each runtime in turn.

A new runtime joins by adding a ``RuntimeCase`` that installs a fake backend
returning a successful or failed run.
"""

from __future__ import annotations

import importlib
import pickle
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from dspy import Prediction
from predict_rlm import RunEvidence, RunTrace

import avalanche as ava
from avalanche.agent.contract import AgentRequest, AgentRuntime, SignatureSpec
from avalanche.runtimes import PredictRLM


class Answer(ava.Signature):
    question: str = ava.InputField()
    answer: str = ava.OutputField()


@dataclass(frozen=True)
class RuntimeCase:
    """``install`` fakes the runtime's backend and returns a log of its builds and calls."""

    make_runtime: Callable[[], AgentRuntime]
    install: Callable[[pytest.MonkeyPatch, bool], list[str]]


def _install_predict_rlm(monkeypatch: pytest.MonkeyPatch, succeed: bool) -> list[str]:
    """Install a fake backend; the returned list records each build and call."""
    calls: list[str] = []

    class Predictor:
        async def acall(self, **inputs):
            calls.append(f"call {inputs['question']}")
            if not succeed:
                error = ValueError("provider unavailable")
                error.evidence = RunEvidence(
                    run_id="run", complete=True, terminal_outcome="error"
                )
                raise error
            return Prediction(
                answer="a",
                trace=RunTrace(
                    status="completed",
                    model="m",
                    iterations=0,
                    max_iterations=1,
                    duration_ms=1,
                ),
                evidence=RunEvidence(run_id="run", complete=True, terminal_outcome="completed"),
            )

    runtime_module = importlib.import_module("avalanche.runtimes.predict_rlm.runtime")

    def build(*args, **kwargs):
        calls.append("build")
        return Predictor()

    monkeypatch.setattr(runtime_module, "_build_predictor", build)
    return calls


CASES = {"predict_rlm": RuntimeCase(PredictRLM, _install_predict_rlm)}


@pytest.fixture(params=sorted(CASES))
def case(request) -> RuntimeCase:
    return CASES[request.param]


class _Recorder:
    """An invocation sink that counts the final trace events it receives."""

    def __init__(self) -> None:
        self.terminals: list[str] = []

    async def emit(self, native: object, common: object) -> None:
        assert not self.terminals, "live event after the final trace"

    def finished(self, header: object, *, outputs: object, complete: bool) -> None:
        self.terminals.append("finished")

    def unavailable(self, **detail: object) -> None:
        self.terminals.append("unavailable")


def _request() -> AgentRequest:
    spec = SignatureSpec.from_dspy(Answer)
    return AgentRequest(
        invocation_id="invocation",
        step_name="answer",
        signature=spec,
        inputs=spec.input_model(question="q"),
        tools=(),
        skills=(),
    )


def test_declaring_a_runtime_builds_nothing_and_it_pickles(case, monkeypatch):
    calls = case.install(monkeypatch, True)

    runtime = case.make_runtime()

    assert pickle.loads(pickle.dumps(runtime)) == runtime
    assert calls == []


@pytest.mark.asyncio
async def test_outcome_trace_names_the_runtime_and_outputs_fit_the_signature(case, monkeypatch):
    case.install(monkeypatch, True)
    runtime = case.make_runtime()
    request = _request()

    recorder = _Recorder()
    outcome = await runtime.invoke(request, recorder)

    assert recorder.terminals == ["finished"]
    assert outcome.trace.header.runtime_kind == runtime.kind
    assert request.signature.validate_outputs(outcome.outputs).model_dump() == {"answer": "a"}


@pytest.mark.asyncio
async def test_a_failed_run_raises_instead_of_returning_an_outcome(case, monkeypatch):
    case.install(monkeypatch, False)

    recorder = _Recorder()
    with pytest.raises(Exception, match="provider unavailable"):
        await case.make_runtime().invoke(_request(), recorder)

    # A failed run still ends with exactly one final trace event.
    assert len(recorder.terminals) == 1
