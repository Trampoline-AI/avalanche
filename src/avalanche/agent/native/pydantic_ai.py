"""Pydantic AI native header, turn and live-event parts, ``pydantic_ai.messages/v1``.

The common layer already carries the run's inputs and outputs and each turn's text,
thinking and tool calls; these models hold what only Pydantic AI reports, mirrored
here without importing it.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt

from ..kinds import RuntimeKind

_STRICT = ConfigDict(
    strict=True,
    frozen=True,
    extra="forbid",
    json_schema_serialization_defaults_required=True,
)

FORMAT = "pydantic_ai.messages/v1"


class PydanticAITurn(BaseModel):
    """The model response behind one turn, as Pydantic AI reports it."""

    model_config = _STRICT

    runtime_kind: ClassVar[RuntimeKind] = "pydantic_ai"

    format: Literal["pydantic_ai.messages/v1"] = FORMAT
    model: str | None
    provider: str | None
    provider_response_id: str | None
    # Normalized by Pydantic AI to "stop", "length", "content_filter", "tool_call" or
    # "error"; ``None`` when the provider gave none.
    finish_reason: str | None

    @property
    def failed(self) -> bool:
        return self.finish_reason == "error"


class PydanticAITraceHeader(BaseModel):
    """Pydantic AI run details that have no common-header equivalent."""

    model_config = _STRICT

    runtime_kind: ClassVar[RuntimeKind] = "pydantic_ai"

    format: Literal["pydantic_ai.messages/v1"] = FORMAT
    # Model requests, as Pydantic AI counts them against ``max_turns``.
    requests: NonNegativeInt
    # Tool and answer calls the model was asked to make again.
    retries: NonNegativeInt
    # Why the run failed, cut to 4096 characters; ``None`` when it succeeded.
    error: str | None


class _Event(BaseModel):
    model_config = _STRICT

    format: Literal["pydantic_ai.messages/v1"] = FORMAT
    runtime_kind: ClassVar[RuntimeKind] = "pydantic_ai"

    @property
    def turn(self) -> int | None:
        return None

    @property
    def error(self) -> str | None:
        return None


class RunInitialized(_Event):
    """The Pydantic AI run as it started: its IDs, model, tools and skills.

    ``tools`` are the function tools Avalanche offers: the step's tools and its file
    tools; toolsets and capabilities given as agent options add their own.
    ``skills`` are the step's Agent Skills, which the model loads by name.
    """

    kind: Literal["run.initialized"] = "run.initialized"

    run_id: str
    conversation_id: str
    model: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]


class ToolRetried(_Event):
    """Pydantic AI asked the model to make a tool or answer call again.

    It travels with the ``tool.finished`` event of the call that is retried.
    """

    kind: Literal["tool.retried"] = "tool.retried"

    call_id: str
    attempt: PositiveInt
    max_retries: NonNegativeInt


PydanticAINativeEvent = Annotated[
    RunInitialized | ToolRetried,
    Field(discriminator="kind"),
]
