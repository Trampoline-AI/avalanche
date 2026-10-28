"""OpenAI Agents SDK native header, turn and live-event parts, ``openai_agents.responses/v1``.

The common layer already carries the run's inputs and outputs and each turn's message,
reasoning and tool calls; these models hold what only the SDK reports, mirrored here
without importing the SDK.
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

FORMAT = "openai_agents.responses/v1"


class OpenAIAgentTurn(BaseModel):
    """The model response behind one turn, as the SDK reports it."""

    model_config = _STRICT

    runtime_kind: ClassVar[RuntimeKind] = "openai_agent"

    format: Literal["openai_agents.responses/v1"] = FORMAT
    response_id: str | None
    model: str
    # Output tokens the model spent reasoning, when the provider reports them.
    reasoning_tokens: NonNegativeInt | None
    # The model declined to answer; the turn then counts as failed.
    refusal: str | None
    # This response carried the run's final answer.
    answer: bool

    @property
    def failed(self) -> bool:
        return self.refusal is not None


class OpenAIAgentTraceHeader(BaseModel):
    """OpenAI Agents SDK run details that have no common-header equivalent."""

    model_config = _STRICT

    runtime_kind: ClassVar[RuntimeKind] = "openai_agent"

    format: Literal["openai_agents.responses/v1"] = FORMAT
    # The most turns the run could take.
    max_turns: PositiveInt
    # Model requests the SDK made, retries included.
    requests: NonNegativeInt
    reasoning_tokens: NonNegativeInt | None
    last_response_id: str | None
    errors: tuple[str, ...]


class _Event(BaseModel):
    model_config = _STRICT

    format: Literal["openai_agents.responses/v1"] = FORMAT
    runtime_kind: ClassVar[RuntimeKind] = "openai_agent"

    @property
    def turn(self) -> int | None:
        return None

    @property
    def error(self) -> str | None:
        return None


class AgentStarted(_Event):
    """The SDK started the step's agent, with the tools it offers the model."""

    kind: Literal["agent.started"] = "agent.started"

    agent_name: str
    model: str | None
    tools: tuple[str, ...]


OpenAIAgentNativeEvent = Annotated[AgentStarted, Field(discriminator="kind")]
