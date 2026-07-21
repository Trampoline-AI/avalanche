"""Validation for agent runtime configuration.

This module is deliberately dependency-free: ``ava.agent`` remains importable
without DSPy or predict-rlm installed.
"""

from __future__ import annotations

from typing import Any, Mapping

UNSET = object()

# These are not agent runtime kwargs. Signatures describe only model inputs and
# outputs; skills and tools belong to the agent-step decorator.
_RESERVED_RUNTIME_KWARGS = frozenset({"signature", "skills", "tools"})


def validate_runtime_kwargs(kwargs: Mapping[str, Any], *, owner: str) -> dict[str, Any]:
    """Return a copy of runtime kwargs or reject agent-definition fields."""
    if not isinstance(kwargs, Mapping):
        raise TypeError(f"{owner} must be a mapping of agent runtime kwargs")
    reserved = sorted(_RESERVED_RUNTIME_KWARGS & set(kwargs))
    if reserved:
        rendered = ", ".join(repr(name) for name in reserved)
        raise TypeError(
            f"{owner} cannot configure {rendered}; pass the signature as the "
            "decorator's first argument and skills/tools on @ava.agent_step(...)."
        )
    return dict(kwargs)
