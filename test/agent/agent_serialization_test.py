"""Agent callables cross the same cloudpickle boundary used by Ray tasks."""

from __future__ import annotations

import asyncio
import importlib

import dspy
import pytest
from dspy import Prediction
from predict_rlm import RunEvidence, RunTrace
from ray import cloudpickle

import avalanche as ava


class EchoSignature(ava.Signature):
    text: str = ava.InputField()
    answer: str = ava.OutputField()


def _agent_node(monkeypatch):
    agent_module = importlib.import_module("avalanche.agent.agent_step")

    class Predictor:
        async def acall(self, *, text):
            return Prediction(
                answer=text.upper(),
                trace=RunTrace(
                    status="completed",
                    model="test-model",
                    iterations=0,
                    max_iterations=1,
                    duration_ms=1,
                ),
                evidence=RunEvidence(
                    run_id="echo-run", complete=True, terminal_outcome="completed"
                ),
            )

    monkeypatch.setattr(agent_module, "_build_predictor", lambda *args, **kwargs: Predictor())

    @ava.agent_step(EchoSignature)
    async def echo(text: str, *, agent: ava.Agent) -> str:
        prediction = await agent(text=text)
        return prediction.answer

    return echo


def test_agent_omitted_skills_and_tools_survive_serialization(monkeypatch):
    node = _agent_node(monkeypatch)
    restored = cloudpickle.loads(cloudpickle.dumps(node.fn))
    assert asyncio.run(restored("retained defaults")) == "RETAINED DEFAULTS"


def test_workflow_bound_agent_callable_survives_serialization(monkeypatch):
    node = _agent_node(monkeypatch)
    bound = node.fn.__agent_step__.with_workflow_defaults(node.fn, {"max_iterations": 2})
    restored = cloudpickle.loads(cloudpickle.dumps(bound))
    assert asyncio.run(restored("bound workflow")) == "BOUND WORKFLOW"


class InlineSplitSignature(ava.Signature):
    text: str = ava.InputField()
    first: str = ava.OutputField()
    count: int = ava.OutputField()


def test_workflow_bound_inline_callable_serializes_without_predictor_or_contextvar(monkeypatch):
    agent_module = importlib.import_module("avalanche.agent.agent_step")

    class Predictor:
        def __init__(self, max_iterations):
            self.max_iterations = max_iterations

        async def acall(self, *, text):
            words = text.split()[: self.max_iterations]
            return dspy.Prediction(
                first=words[0].upper(),
                count=len(words),
                trace=RunTrace(
                    status="completed",
                    model="test-model",
                    iterations=0,
                    max_iterations=self.max_iterations,
                    duration_ms=1,
                ),
                evidence=RunEvidence(
                    run_id="inline-run", complete=True, terminal_outcome="completed"
                ),
            )

    @ava.workflow(agent_defaults={"max_iterations": 2})
    def flow():
        return ava.agent.step(InlineSplitSignature, inputs={"text": "red green blue"})

    def forbidden_build(*args, **kwargs):
        raise AssertionError("declaration and serialization must not create a predictor")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(agent_module, "_build_predictor", forbidden_build)
        workflow = flow()
        node = next(iter(workflow.nodes.values())).node
        bound = node.fn.__agent_step__.with_workflow_defaults(node.fn, workflow.agent_defaults)
        serialized_callable = cloudpickle.dumps(bound)
        serialized_workflow = cloudpickle.dumps(workflow)

    monkeypatch.setattr(
        agent_module,
        "_build_predictor",
        lambda signature, **config: Predictor(config["max_iterations"]),
    )
    restored = cloudpickle.loads(serialized_callable)
    assert asyncio.run(restored(text="blue yellow pink")) == ("BLUE", 2)
    restored_workflow = cloudpickle.loads(serialized_workflow)
    assert restored_workflow.run(executor=ava.LocalExecutor()).result() == ("RED", 2)
