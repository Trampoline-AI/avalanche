"""Avalanche agent integration.

Importing this package does not import dspy or predict_rlm; both are pulled
in lazily at execution / signature-generation time.
"""

from avalanche.agent.agent_step import (
    AgentStepError,
    AgentStepExecutionError,
    agent_step,
)
from avalanche.agent.config import (
    AgentConfig,
    configure_agent,
    get_agent_config,
    reset_agent_config,
)
from avalanche.agent.desc import Desc
from avalanche.agent.signature import generate_signature

__all__ = [
    "AgentConfig",
    "AgentStepError",
    "AgentStepExecutionError",
    "Desc",
    "agent_step",
    "configure_agent",
    "generate_signature",
    "get_agent_config",
    "reset_agent_config",
    "skills",
]


def __getattr__(name: str):
    if name == "skills":
        import predict_rlm.skills as skills

        globals()["skills"] = skills
        return skills
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
