"""Agent runtimes that run Avalanche agent steps.

Each runtime loads on first use, so a runtime's SDK is imported only when that
runtime is used.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import claude_agent, openai_agent, predict_rlm, pydantic_ai
    from .claude_agent import ClaudeAgent
    from .openai_agent import OpenAIAgent
    from .predict_rlm import PredictRLM
    from .pydantic_ai import PydanticAIAgent

__all__ = [
    "ClaudeAgent",
    "OpenAIAgent",
    "PredictRLM",
    "PydanticAIAgent",
    "claude_agent",
    "openai_agent",
    "predict_rlm",
    "pydantic_ai",
]


def __getattr__(name: str):
    if name == "PredictRLM":
        from . import predict_rlm

        value = predict_rlm.PredictRLM
    elif name == "ClaudeAgent":
        from . import claude_agent

        value = claude_agent.ClaudeAgent
    elif name == "OpenAIAgent":
        from . import openai_agent

        value = openai_agent.OpenAIAgent
    elif name == "PydanticAIAgent":
        from . import pydantic_ai

        value = pydantic_ai.PydanticAIAgent
    elif name in {"claude_agent", "openai_agent", "predict_rlm", "pydantic_ai"}:
        value = importlib.import_module(f".{name}", __name__)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = value
    return value
