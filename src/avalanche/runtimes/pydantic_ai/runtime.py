"""The Pydantic AI runtime descriptor.

Building a ``PydanticAIAgent`` needs no Pydantic AI install: the library is used
only when a step runs, so workflows that declare Pydantic AI steps import and
publish their declarations without the ``pydantic-ai`` extra installed.
"""

from __future__ import annotations

import inspect
from collections.abc import Collection, Mapping
from types import MappingProxyType
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, PositiveInt, model_validator

from ...agent.contract import AgentEventSink, AgentRequest, AgentRunOutcome
from ...agent.declaration import PydanticAIDeclaration
from ...agent.steps import AgentStepExecutionError
from ...agent.trace import RuntimeKind

# Agent arguments a step cannot set, because Avalanche fills them from the step,
# with what to use instead.
_REPLACED_OPTIONS: Mapping[str, str] = MappingProxyType(
    {
        "output_type": "the step signature's outputs define the answer",
        "instructions": "the step's instructions are the agent's instructions; add to them "
        "with instructions_append",
        "system_prompt": "the step's instructions are the agent's instructions; add to them "
        "with instructions_append",
        "deps_type": "agent steps run without Pydantic AI dependencies; give tools what they "
        "need as step inputs or closures",
    }
)


def unknown_agent_options(names: Collection[str], known: Collection[str]) -> str | None:
    """Why ``names`` are not all ``pydantic_ai.Agent`` arguments, or ``None`` when they are."""
    unknown = sorted(set(names) - set(known))
    if not unknown:
        return None
    return (
        f"PydanticAIAgent got unknown options {unknown}: other than its own settings, "
        "PydanticAIAgent takes only arguments of pydantic_ai.Agent"
    )


def agent_option_names(agent: type) -> frozenset[str]:
    """The arguments ``pydantic_ai.Agent`` takes by keyword, apart from its model."""
    return frozenset(inspect.signature(agent).parameters) - {"self", "model"}


def _installed_option_names() -> frozenset[str] | None:
    """The Agent's argument names, or ``None`` without the ``pydantic-ai`` extra."""
    try:
        from pydantic_ai import Agent
    except ModuleNotFoundError as exc:
        if exc.name != "pydantic_ai":
            raise
        return None
    return agent_option_names(Agent)


class PydanticAIAgent(BaseModel):
    """Run agent steps with any model Pydantic AI supports.

    ``model`` is a Pydantic AI model name, ``provider:model``, such as
    ``"openai:gpt-5.4-nano"`` or ``"anthropic:claude-haiku-5-5"``; the provider
    reads its credentials from its usual environment variables, such as
    ``OPENAI_API_KEY``. ``max_turns`` is the most model requests one call may
    make; ``None`` keeps Pydantic AI's own limit. ``instructions_append`` is added
    after the step's instructions.

    Any other keyword is a ``pydantic_ai.Agent`` argument, such as
    ``model_settings``, ``retries`` or ``capabilities``. ``tools``, ``toolsets`` and
    ``capabilities`` are added to what Avalanche sets for the step; any other
    option is passed as given. ``output_type``, ``instructions``,
    ``system_prompt`` and ``deps_type`` are rejected. An unknown name fails here
    when Pydantic AI is installed, and when the step runs otherwise. These options
    are never published or recorded: the declaration lists their names only.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="allow")

    kind: ClassVar[RuntimeKind] = "pydantic_ai"

    model: str | None = None
    max_turns: PositiveInt | None = None
    instructions_append: str | None = None

    @model_validator(mode="after")
    def _check_agent_options(self) -> Self:
        names = self.agent_options.keys()
        for name in names:
            if name in _REPLACED_OPTIONS:
                raise ValueError(
                    f"PydanticAIAgent does not take {name!r}: {_REPLACED_OPTIONS[name]}"
                )
        known = _installed_option_names() if names else None
        if known is not None and (problem := unknown_agent_options(names, known)):
            raise ValueError(problem)
        return self

    @property
    def agent_options(self) -> Mapping[str, Any]:
        """The ``pydantic_ai.Agent`` arguments given by keyword, by name, unchecked."""
        # Values are Pydantic AI's own types, checked by Pydantic AI.
        extra = self.__pydantic_extra__
        assert extra is not None, "PydanticAIAgent allows extra fields"
        return MappingProxyType(extra)

    def merged_over(self, base: PydanticAIAgent) -> PydanticAIAgent:
        """This runtime with the fields it leaves unset taken from ``base``."""
        # Iterating a model gives its fields and its extra ``pydantic_ai.Agent`` options.
        inherited, own = dict(base), dict(self)
        fields = {name: inherited[name] for name in base.model_fields_set}
        fields.update({name: own[name] for name in self.model_fields_set})
        return PydanticAIAgent.model_validate(fields)

    def declaration(self, *, workflow_default: PydanticAIAgent | None) -> PydanticAIDeclaration:
        """Describe this step runtime merged over the workflow default, statically."""
        effective = self if workflow_default is None else self.merged_over(workflow_default)
        return PydanticAIDeclaration(
            model=effective.model,
            settings={
                "max_turns": effective.max_turns,
                "instructions_append": effective.instructions_append,
            },
            agent_options=tuple(sorted(effective.agent_options)),
        )

    async def invoke(self, request: AgentRequest, events: AgentEventSink) -> AgentRunOutcome:
        try:
            from . import _sdk
        except ModuleNotFoundError as exc:
            if exc.name != "pydantic_ai":
                raise
            raise AgentStepExecutionError(
                f"agent step {request.step_name!r} runs on ava.runtimes.PydanticAIAgent, "
                "which needs Pydantic AI: install avalanche-ai[pydantic-ai]"
            ) from exc
        return await _sdk.run(self, request, events)
