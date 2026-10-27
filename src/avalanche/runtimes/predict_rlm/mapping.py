"""Map PredictRLM's run records onto the common agent trace layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...agent.native import predict_rlm as native
from ...agent.trace import TokenUsage, ToolCall, Turn, evidence_value
from .events import native_token_usage, native_usage

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


def step_tool_call_count(step: IterationStep) -> int:
    """The step's tool calls, its ``predict`` sub-model calls included."""
    return len(step.tool_calls) + sum(len(group.calls) for group in step.predict_calls)


def turn_from_step(step: IterationStep) -> Turn:
    """One PredictRLM iteration: the tools and sub-model calls its code made, then its code."""
    tool_calls = [
        ToolCall(
            call_id=None,
            caller="code",
            name=tool.name,
            arguments={
                "args": evidence_value(tool.args),
                "kwargs": evidence_value(tool.kwargs),
            },
            result=evidence_value(tool.result),
            error=tool.error,
            duration_ms=tool.duration_ms,
        )
        for tool in step.tool_calls
    ]
    # Each sub-model call is a ``predict`` tool call, its native part in the same order.
    predict_calls: list[native.PredictRLMPredictCall] = []
    for group in step.predict_calls:
        for call in group.calls:
            tool_calls.append(
                ToolCall(
                    call_id=None,
                    caller="code",
                    name="predict",
                    arguments=evidence_value(call.input),
                    result=evidence_value(call.output),
                    error=call.error,
                    duration_ms=call.duration_ms,
                )
            )
            predict_calls.append(
                native.PredictRLMPredictCall(
                    signature=group.signature,
                    instructions=group.instructions,
                    model=group.model,
                    usage=native_token_usage(call.usage),
                    finish_reason=call.lm.finish_reason if call.lm is not None else None,
                )
            )
    return Turn(
        index=step.iteration,
        # PredictRLM's "reasoning" is what the model writes each iteration, the turn's
        # message. Provider reasoning is not captured yet.
        message=step.reasoning or None,
        reasoning=None,
        tool_calls=tuple(tool_calls),
        duration_ms=step.duration_ms,
        usage=common_usage(step.usage.main, step.usage.sub),
        native=native.PredictRLMTurn(
            code=step.code,
            output=step.output,
            untruncated_output=step.untruncated_output,
            error=step.error,
            finish_reason=step.lm.finish_reason if step.lm is not None else None,
            usage=native_usage(step.usage),
            predict_calls=tuple(predict_calls),
        ),
    )
