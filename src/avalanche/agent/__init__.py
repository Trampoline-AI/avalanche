"""Avalanche agent integration.

Importing this package does not import dspy or predict_rlm.
"""

from avalanche.agent.desc import Desc
from avalanche.agent.signature import generate_signature

__all__ = ["Desc", "generate_signature", "skills"]


def __getattr__(name: str):
    if name == "skills":
        import predict_rlm.skills as skills

        globals()["skills"] = skills
        return skills
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
