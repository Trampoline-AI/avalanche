"""Avalanche agent integration."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from predict_rlm import DirectPythonBackend, Skill, skills
    from predict_rlm import RunTrace as AgentTrace

    from ..file import File
    from .defaults import AgentDefaults
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
    from .steps import Agent, AgentStepError, AgentStepExecutionError, agent_step, step

# Exports load on first use, so runtime-neutral submodules such as
# ``declaration`` and ``trace`` import without DSPy or PredictRLM.
_EXPORTS = {
    "Agent": ".steps",
    "AgentStepError": ".steps",
    "AgentStepExecutionError": ".steps",
    "agent_step": ".steps",
    "step": ".steps",
    "AgentEvidenceEvent": ".evidence",
    "AgentEvidenceListener": ".evidence",
    "AgentEvidenceObserverEvent": ".evidence",
    "AgentInvocationId": ".evidence",
    "AgentTraceFinishedEvent": ".evidence",
    "AgentTraceUnavailableEvent": ".evidence",
    "ListenerErrorPolicy": ".evidence",
    "capture_agent_evidence": ".evidence",
    "AgentDefaults": ".defaults",
    "InputField": ".signature",
    "OutputField": ".signature",
    "Signature": ".signature",
}

__all__ = [
    "Agent",
    "AgentDefaults",
    "AgentEvidenceEvent",
    "AgentEvidenceListener",
    "AgentEvidenceObserverEvent",
    "AgentInvocationId",
    "AgentStepError",
    "AgentStepExecutionError",
    "AgentTrace",
    "AgentTraceFinishedEvent",
    "AgentTraceUnavailableEvent",
    "DirectPythonBackend",
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
    if name in _EXPORTS:
        value = getattr(importlib.import_module(_EXPORTS[name], __name__), name)
    elif name == "skills":
        import predict_rlm.skills as value
    elif name == "AgentTrace":
        from predict_rlm import RunTrace

        value = RunTrace
    elif name == "File":
        import warnings

        from avalanche.file import File

        warnings.warn(
            "ava.agent.File is deprecated; use ava.File. Removed next minor release.",
            DeprecationWarning, stacklevel=2,
        )
        return File
    elif name == "DirectPythonBackend":
        from predict_rlm import DirectPythonBackend

        value = DirectPythonBackend
    elif name == "Skill":
        import predict_rlm

        value = getattr(predict_rlm, name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = value
    return value
