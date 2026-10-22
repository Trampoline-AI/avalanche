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
    make_runtime: Callable[[], AgentRuntime]
    install: Callable[[pytest.MonkeyPatch, bool], list[str]]


def _install_predict_rlm(monkeypatch: pytest.MonkeyPatch, succeed: bool) -> list[str]:
    calls: list[str] = []

    class Predictor:
        async def acall(self, **inputs):
            calls.append(inputs["question"])
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
    monkeypatch.setattr(runtime_module, "_build_predictor", lambda *args, **kwargs: Predictor())
    return calls


CASES = {"predict_rlm": RuntimeCase(PredictRLM, _install_predict_rlm)}


@pytest.fixture(params=sorted(CASES))
def case(request) -> RuntimeCase:
    return CASES[request.param]


class _Recorder:
    async def emit(self, event: object) -> None:
        return None


def _request() -> AgentRequest:
    spec = SignatureSpec.from_dspy(Answer)
    return AgentRequest(
        invocation_id="invocation",
        step_name="answer",
        signature=spec,
        inputs=spec.input_model(question="q"),
        tools=(),
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

    outcome = await runtime.invoke(request, _Recorder())

    assert outcome.trace.header.runtime_kind == runtime.kind
    assert request.signature.validate_outputs(outcome.outputs).model_dump() == {"answer": "a"}


@pytest.mark.asyncio
async def test_a_failed_run_raises_instead_of_returning_an_outcome(case, monkeypatch):
    case.install(monkeypatch, False)

    with pytest.raises(Exception, match="provider unavailable"):
        await case.make_runtime().invoke(_request(), _Recorder())
