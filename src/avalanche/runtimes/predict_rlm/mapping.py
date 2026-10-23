"""Map PredictRLM's run records onto the common agent trace layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...agent.contract import (
    AgentEvent,
    CodeGenerated,
    ModelFinished,
    ModelStarted,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
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
    from predict_rlm import IterationStep, RunEvent
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
        text=None,
        reasoning=step.reasoning,
        actions=tuple(actions),
        duration_ms=step.duration_ms,
        usage=common_usage(step.usage.main, step.usage.sub),
    )


# PredictRLM event kinds that have a common live event; the rest are native only.
COMMON_EVENT_KINDS = {
    "iteration.recorded": "turn.finished",
    "predict.started": "model.started",
    "predict.finished": "model.finished",
    "tool.started": "tool.started",
    "tool.finished": "tool.finished",
    "code.generated": "code.generated",
}


def common_event(event: RunEvent) -> AgentEvent | None:
    """The common live event for a PredictRLM event; run lifecycle has none."""
    data = event.data
    match event.kind.value:
        case "iteration.recorded":
            from predict_rlm import IterationStep

            step = IterationStep.model_validate(data["step"], strict=True)
            return TurnFinished(turn=turn_from_step(step))
        case "predict.started":
            return ModelStarted(call_id=data["call_id"], model=data["model"])
        case "predict.finished":
            return ModelFinished(call_id=data["call_id"], error=data.get("error"))
        case "tool.started":
            return ToolStarted(call_id=data["call_id"], name=data["name"])
        case "tool.finished":
            return ToolFinished(
                call_id=data["call_id"], name=data["name"], error=data.get("error")
            )
        case "code.generated":
            return CodeGenerated(turn=data["iteration"], code=data["code"])
        case _:
            return None
