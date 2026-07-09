"""Validation for agent runtime configuration.

This module is deliberately dependency-free: ``ava.agent`` remains importable
without DSPy or predict-rlm installed.
"""

from __future__ import annotations

from typing import Any, Mapping

UNSET = object()

# These define what an agent knows or can do. They belong to Signature, not a
# workflow's or step's execution configuration.
_RESERVED_RUNTIME_KWARGS = frozenset({"signature", "skills", "tools"})


def validate_runtime_kwargs(kwargs: Mapping[str, Any], *, owner: str) -> dict[str, Any]:
    """Return a copy of runtime kwargs or reject agent-definition fields."""
    if not isinstance(kwargs, Mapping):
        raise TypeError(f"{owner} must be a mapping of PredictRLM runtime kwargs")
    reserved = sorted(_RESERVED_RUNTIME_KWARGS & set(kwargs))
    if reserved:
        rendered = ", ".join(repr(name) for name in reserved)
        raise TypeError(
            f"{owner} cannot configure {rendered}; define those on "
            "ava.agent.Signature instead."
        )
    return dict(kwargs)
