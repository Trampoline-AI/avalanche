"""Agent runtimes that run Avalanche agent steps.

Each runtime loads on first use, so a runtime's SDK is imported only when that
runtime is used.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import claude_agent, predict_rlm
    from .claude_agent import ClaudeAgent
    from .predict_rlm import PredictRLM

__all__ = ["ClaudeAgent", "PredictRLM", "claude_agent", "predict_rlm"]


def __getattr__(name: str):
    if name == "PredictRLM":
        from . import predict_rlm

        value = predict_rlm.PredictRLM
    elif name == "ClaudeAgent":
        from . import claude_agent

        value = claude_agent.ClaudeAgent
    elif name in {"claude_agent", "predict_rlm"}:
        value = importlib.import_module(f".{name}", __name__)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = value
    return value
