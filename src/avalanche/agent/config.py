"""Global defaults for agent steps.

``configure_agent`` sets process-wide defaults consumed by ``@ava.agent_step``.
Decorator kwargs always take precedence over these globals, key by key.

This module imports neither dspy nor predict_rlm.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from ..storage import Namespace

_UNSET = object()


@dataclass(frozen=True)
class AgentConfig:
    """Resolved global agent-step defaults."""

    lm: Any | None = None
    sub_lm: Any | None = None
    skills: Sequence[Any] | None = None
    max_iterations: int | None = None
    namespace: "Namespace | None" = None


_config = AgentConfig()


def configure_agent(
    *,
    lm: Any = _UNSET,
    sub_lm: Any = _UNSET,
    skills: Sequence[Any] | None = _UNSET,
    max_iterations: int | None = _UNSET,
    namespace: "Namespace | None" = _UNSET,
) -> None:
    """Set global defaults for agent steps.

    Only the keyword arguments passed are updated; other defaults are kept.
    Per-step decorator kwargs override these globals key by key.
    """
    global _config
    updates = {
        name: value
        for name, value in (
            ("lm", lm),
            ("sub_lm", sub_lm),
            ("skills", skills),
            ("max_iterations", max_iterations),
            ("namespace", namespace),
        )
        if value is not _UNSET
    }
    _config = replace(_config, **updates)


def get_agent_config() -> AgentConfig:
    """Return the current global agent-step defaults."""
    return _config


def reset_agent_config() -> None:
    """Clear all global agent-step defaults (test seam)."""
    global _config
    _config = AgentConfig()
