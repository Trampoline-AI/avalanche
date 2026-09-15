"""Agent callables cross the same cloudpickle boundary used by Ray tasks."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

from ray import cloudpickle

import avalanche as ava


class EchoSignature(ava.Signature):
    text: str = ava.InputField()
    answer: str = ava.OutputField()


def _agent_node(monkeypatch):
    agent_module = importlib.import_module("avalanche.agent.agent_step")

    class Predictor:
        async def acall(self, *, text):
            return SimpleNamespace(answer=text.upper())

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
