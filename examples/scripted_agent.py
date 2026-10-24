"""Three deterministic agent iterations, without API keys or network LM calls.

Run: uv run python examples/scripted_agent.py
Inspect: uv run ava operator examples/scripted_agent.py
Then: uv run ava tui --connect localhost:7433 --flow scripted_agent
"""

from __future__ import annotations

import dspy
from dspy import JSONAdapter
from dspy.utils import DummyLM

import avalanche as ava
from avalanche.agent import Agent


class SumSignature(ava.Signature):
    """Compute and verify a sum by executing Python before submitting it."""

    task: str = ava.InputField()
    answer: int = ava.OutputField()


@ava.agent_step(SumSignature, runtime=ava.runtimes.PredictRLM(max_iterations=3))
async def sum_numbers(*, agent: Agent) -> dict[str, int]:
    # PredictRLM's action schema uses 'plan', not 'reasoning'. JSONAdapter makes
    # DummyLM serialize each action as JSON, exactly like a structured LM reply.
    lm = DummyLM(
        [
            {"plan": "Prepare the data", "code": "numbers = [2, 3, 5]\nprint(numbers)"},
            {"plan": "Compute the sum", "code": "total = sum(numbers)\nprint(total)"},
            {"plan": "Verify and submit", "code": "assert total == 10\nSUBMIT(answer=total)"},
        ],
        adapter=JSONAdapter(),
    )
    with dspy.context(lm=lm, adapter=JSONAdapter()):
        result = await agent(task="Sum 2, 3 and 5; verify the result.")
    assert result.outputs.answer == 10
    assert len(lm.history) == 3
    return {"answer": result.outputs.answer, "lm_calls": len(lm.history)}


@ava.workflow
def scripted_agent():
    return sum_numbers()


if __name__ == "__main__":
    answer = scripted_agent().run(executor=ava.LocalExecutor()).result()
    print(answer)
