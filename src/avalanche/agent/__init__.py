"""Avalanche agent integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .agent_step import Agent, AgentStepError, AgentStepExecutionError, agent_step, step
from .evidence import (
    AgentEvidenceEvent,
    AgentEvidenceListener,
    AgentEvidenceObserverEvent,
    AgentInvocationId,
    AgentTraceFinishedEvent,
    AgentTraceUnavailableEvent,
    ListenerErrorPolicy,
    capture_agent_evidence,
)
from .signature import InputField, OutputField, Signature

if TYPE_CHECKING:
    from predict_rlm import RunTrace as AgentTrace

__all__ = [
    "Agent",
    "AgentEvidenceEvent",
    "AgentEvidenceListener",
    "AgentEvidenceObserverEvent",
    "AgentInvocationId",
    "AgentStepError",
    "AgentStepExecutionError",
    "AgentTrace",
    "AgentTraceFinishedEvent",
    "AgentTraceUnavailableEvent",
    "File",
    "InputField",
    "ListenerErrorPolicy",
    "OutputField",
    "Signature",
    "Skill",
    "agent_step",
    "capture_agent_evidence",
    "skills",
    "step",
]


def __getattr__(name: str):
    if name == "skills":
        import predict_rlm.skills as value
    elif name == "AgentTrace":
        from predict_rlm import RunTrace

        value = RunTrace
    elif name in {"Skill", "File"}:
        import predict_rlm

        value = getattr(predict_rlm, name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = value
    return value
