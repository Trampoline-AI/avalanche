"""Agent runtimes that run Avalanche agent steps.

Each runtime loads on first use, so a runtime's SDK is imported only when that
runtime is used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .predict_rlm import PredictRLM

__all__ = ["PredictRLM"]


def __getattr__(name: str):
    if name == "PredictRLM":
        from . import predict_rlm

        value = predict_rlm.PredictRLM
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = value
    return value
