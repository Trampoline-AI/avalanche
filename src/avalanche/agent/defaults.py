"""Workflow-wide agent defaults and the effective runtime of an agent step."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .contract import AgentRuntime


@dataclass(frozen=True)
class AgentDefaults:
    """Agent settings shared by every agent step of a workflow.

    A step's runtime of the same kind is merged over ``runtime`` field by field;
    a step runtime of another kind replaces it.
    """

    runtime: AgentRuntime | None = None

    def __post_init__(self) -> None:
        if self.runtime is not None and not isinstance(self.runtime, AgentRuntime):
            raise TypeError(
                "agent_defaults runtime must be an agent runtime such as "
                f"ava.runtimes.PredictRLM(...), not {type(self.runtime).__name__}"
            )


def agent_defaults_from(value: AgentDefaults | Mapping[str, object]) -> AgentDefaults:
    """Validate ``ava.workflow(agent_defaults=...)``."""
    if isinstance(value, AgentDefaults):
        return value
    if not isinstance(value, Mapping):
        raise TypeError(
            "ava.workflow(agent_defaults=...) must be a mapping such as "
            "{'runtime': ava.runtimes.PredictRLM(...)}"
        )
    unknown = sorted(set(value) - {"runtime"})
    if unknown:
        raise TypeError(
            f"ava.workflow(agent_defaults=...) no longer accepts {_names(unknown)}; "
            "set them on the runtime instead: "
            f"agent_defaults={{'runtime': ava.runtimes.PredictRLM({_assignments(unknown)})}}"
        )
    runtime = value.get("runtime")
    if runtime is not None and not isinstance(runtime, AgentRuntime):
        raise TypeError(
            "agent_defaults runtime must be an agent runtime such as "
            f"ava.runtimes.PredictRLM(...), not {type(runtime).__name__}"
        )
    return AgentDefaults(runtime=runtime)


def reject_removed_arguments(owner: str, removed: Mapping[str, object]) -> None:
    """Fail on agent-step arguments that moved to the runtime, naming the replacement."""
    if removed:
        names = sorted(removed)
        raise TypeError(
            f"{owner} no longer accepts {_names(names)}; pass them to the runtime: "
            f"runtime=ava.runtimes.PredictRLM({_assignments(names)})"
        )


def effective_runtime(step: AgentRuntime | None, defaults: AgentDefaults) -> AgentRuntime:
    """The runtime an agent step runs on: its own, the workflow's, or PredictRLM.

    The result is always a new instance: runtimes cache what they build, and
    that cache must live and die with one agent, not with the declaration.
    """
    default = defaults.runtime
    if step is None:
        if default is not None:
            return default.merged_over(default)
        from ..runtimes.predict_rlm import PredictRLM

        return PredictRLM()
    if default is not None and default.kind == step.kind:
        return step.merged_over(default)
    return step.merged_over(step)


def _names(names: list[str]) -> str:
    return ", ".join(f"{name}=" for name in names)


def _assignments(names: list[str]) -> str:
    return ", ".join(f"{name}=..." for name in names)
