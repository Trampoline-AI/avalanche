"""Map PredictRLM's run records onto the common agent trace layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...agent.trace import (
    AgentAction,
    CodeExecutionAction,
    ModelCallAction,
    TokenUsage,
    ToolCallAction,
    Turn,
)
from .events import _project_agent_value

if TYPE_CHECKING:
    from predict_rlm import IterationStep
    from predict_rlm.trace import TokenUsage as PredictRLMTokenUsage


def common_usage(*usages: PredictRLMTokenUsage) -> TokenUsage:
    """Sum PredictRLM usage records; cache hits are a count and stay native."""
    return TokenUsage(
        input_tokens=sum(usage.input_tokens for usage in usages),
        output_tokens=sum(usage.output_tokens for usage in usages),
        cost_usd=sum(usage.cost for usage in usages),
    )


def turn_from_step(step: IterationStep) -> Turn:
    """One PredictRLM iteration: its code, the tools and sub-model calls that code made."""
    actions: list[AgentAction] = [
        CodeExecutionAction(
            language="python",
            code=step.code,
            output=step.output,
            error=step.output if step.error else None,
        )
    ]
    actions.extend(
        ToolCallAction(
            call_id=None,
            caller="code",
            name=tool.name,
            arguments={
                "args": _project_agent_value(tool.args),
                "kwargs": _project_agent_value(tool.kwargs),
            },
            result=_project_agent_value(tool.result),
            error=tool.error,
            duration_ms=tool.duration_ms,
        )
        for tool in step.tool_calls
    )
    actions.extend(
        ModelCallAction(
            call_id=None,
            caller="code",
            model=group.model,
            duration_ms=call.duration_ms,
            usage=common_usage(call.usage),
            error=call.error,
        )
        for group in step.predict_calls
        for call in group.calls
    )
    return Turn(
        index=step.iteration,
        # PredictRLM's "reasoning" is what the model writes each iteration, the turn's
        # text. Provider reasoning is not captured yet.
        text=step.reasoning or None,
        reasoning=None,
        actions=tuple(actions),
        duration_ms=step.duration_ms,
        usage=common_usage(step.usage.main, step.usage.sub),
    )
