"""The OpenAI Agents SDK runtime descriptor.

Building an ``OpenAIAgent`` does not need the SDK: it is used only when a step
runs, so workflows that declare OpenAI steps import and publish their
declarations without the ``openai`` extra installed.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Sequence
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, PositiveInt, model_validator

from ...agent.contract import AgentEventSink, AgentRequest, AgentRunOutcome
from ...agent.declaration import OpenAIAgentDeclaration, OpenAIBuiltinTool
from ...agent.steps import AgentStepExecutionError
from ...agent.trace import RuntimeKind

# The SDK's own limit when a run sets none.
DEFAULT_MAX_TURNS = 10


def unknown_model_settings(names: Collection[str], known: Collection[str]) -> str | None:
    """Why ``names`` are not all SDK model settings, or ``None`` when they are."""
    unknown = sorted(set(names) - set(known))
    if not unknown:
        return None
    return (
        f"OpenAIAgent got unknown options {unknown}: other than its own settings, "
        "OpenAIAgent takes only fields of agents.ModelSettings"
    )


def _model_setting_names() -> frozenset[str] | None:
    """The SDK's model setting names, or ``None`` without the ``openai`` extra."""
    try:
        from agents import ModelSettings
    except ModuleNotFoundError as exc:
        if exc.name != "agents":
            raise
        return None
    return frozenset(field.name for field in dataclasses.fields(ModelSettings))


class OpenAIAgent(BaseModel):
    """Run agent steps with OpenAI models through the OpenAI Agents SDK.

    ``model`` selects the model, such as ``"gpt-5.4-mini"``; ``None`` lets the SDK
    choose (``OPENAI_DEFAULT_MODEL``, else its own default). ``max_turns`` is the
    most model responses one call may take, 10 when unset. The step's tools are
    always available. ``builtin_tools`` turns on the SDK's sandbox tools:
    ``"filesystem"`` edits files with ``apply_patch`` and views images, ``"shell"``
    runs commands. None are enabled by default. With either, the agent works in a
    local sandbox rooted at the step's ``ava.Workspace`` input, edited in place, or
    else at a temporary folder; on Linux its commands are not confined. With
    ``narrate`` on, the instructions ask the model to note what it is about to do
    before each tool call, and its answer must carry a ``run_summary`` that the
    trace shows on the answering turn. ``tracing`` sends the SDK's own trace of
    each call to OpenAI; it is off by default.

    The SDK reads ``OPENAI_API_KEY`` (and ``OPENAI_BASE_URL``) from the environment
    of the process that runs the step, Ray workers included.

    Any other keyword is an ``agents.ModelSettings`` field passed to the SDK, such
    as ``temperature``, ``reasoning`` or ``max_tokens``. An unknown name fails here
    when the SDK is installed, and when the step runs otherwise. These options are
    never published or recorded.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="allow")

    kind: ClassVar[RuntimeKind] = "openai_agent"

    model: str | None = None
    max_turns: PositiveInt | None = None
    builtin_tools: Sequence[OpenAIBuiltinTool] = ()
    system_prompt_append: str | None = None
    narrate: bool = True
    tracing: bool = False

    @model_validator(mode="after")
    def _check_model_settings(self) -> Self:
        names = self.model_setting_names
        # Importing the SDK is slow: only a runtime with model settings pays for it.
        known = _model_setting_names() if names else None
        if known is not None and (problem := unknown_model_settings(names, known)):
            raise ValueError(problem)
        return self

    @property
    def model_setting_names(self) -> frozenset[str]:
        """The ``agents.ModelSettings`` fields given by keyword.

        Their values, the SDK's own types, are in ``model_extra``; the SDK and the API
        check them.
        """
        extra = self.model_extra
        assert extra is not None, "OpenAIAgent allows extra fields"
        return frozenset(extra)

    def merged_over(self, base: OpenAIAgent) -> OpenAIAgent:
        """This runtime with the fields it leaves unset taken from ``base``."""
        # Iterating keeps model settings as the SDK objects they were given as.
        fields = {name: value for name, value in base if name in base.model_fields_set}
        fields.update((name, value) for name, value in self if name in self.model_fields_set)
        return OpenAIAgent.model_validate(fields)

    def declaration(self, *, workflow_default: OpenAIAgent | None) -> OpenAIAgentDeclaration:
        """Describe this step runtime merged over the workflow default, statically."""
        effective = self if workflow_default is None else self.merged_over(workflow_default)
        return OpenAIAgentDeclaration(
            model=effective.model,
            settings={
                "max_turns": effective.max_turns,
                "system_prompt_append": effective.system_prompt_append,
                "narrate": effective.narrate,
                "tracing": effective.tracing,
            },
            builtin_tools=tuple(effective.builtin_tools),
        )

    async def invoke(self, request: AgentRequest, events: AgentEventSink) -> AgentRunOutcome:
        try:
            from . import _sdk
        except ModuleNotFoundError as exc:
            if exc.name != "agents":
                raise
            raise AgentStepExecutionError(
                f"agent step {request.step_name!r} runs on ava.runtimes.OpenAIAgent, which "
                "needs the OpenAI Agents SDK: install avalanche-ai[openai]"
            ) from exc
        return await _sdk.run(self, request, events)
