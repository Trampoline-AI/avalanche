"""A run's topology names each agent node's runtime, workflow defaults applied."""

from __future__ import annotations

import avalanche as ava
from runtime.operator.convert_v2 import workflow_topology_from_v2, workflow_topology_to_v2
from runtime.operator.models import WorkflowTopology
from runtime.operator.registry import agent_runtime_kinds_for_workflow


class Echo(ava.Signature):
    """Echo the text."""

    text: str = ava.InputField()
    echoed: str = ava.OutputField()


@ava.step
def load() -> str:
    return "hi"


@ava.agent_step(Echo, runtime=ava.runtimes.ClaudeAgent(model="claude-haiku-5-5"))
async def on_claude(text: str, *, agent: ava.Agent) -> str:
    return (await agent(text=text)).outputs.echoed


@ava.agent_step(Echo)
async def on_default(text: str, *, agent: ava.Agent) -> str:
    return (await agent(text=text)).outputs.echoed


@ava.agent_step(Echo, runtime=ava.runtimes.PredictRLM(max_iterations=3))
async def on_predict_rlm(text: str, *, agent: ava.Agent) -> str:
    return (await agent(text=text)).outputs.echoed


@ava.workflow(agent_defaults={"runtime": ava.runtimes.ClaudeAgent()})
def mixed():
    text = load()
    return on_claude(text) & on_default(text) & on_predict_rlm(text)


def test_agent_runtime_kinds_follow_each_step_and_the_workflow_default():
    workflow = mixed()
    node_ids = workflow._topological_sort()

    kinds = agent_runtime_kinds_for_workflow(workflow, node_ids)

    # Standard steps have no runtime; a step without one runs on the workflow default.
    assert kinds == {
        "on_claude_1": "claude_agent",
        "on_default_1": "claude_agent",
        "on_predict_rlm_1": "predict_rlm",
    }
    topology = WorkflowTopology(
        node_ids=tuple(node_ids), agent_runtime_kinds=tuple(sorted(kinds.items()))
    )
    restored = workflow_topology_from_v2(workflow_topology_to_v2(topology))
    assert restored.agent_runtime_kinds == topology.agent_runtime_kinds
