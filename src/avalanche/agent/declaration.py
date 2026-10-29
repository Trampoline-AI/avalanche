"""Static description of an agent step, published by discovery.

A declaration says what an agent step is without building or running it: its
signature, its tools, and its runtime's settings. The runtime section is a
closed union keyed by ``kind`` and is defined here without SDK imports, so the
operator, client and TUI can validate every runtime's declaration without that
runtime's extra installed. Credentials never enter a declaration.
"""

from __future__ import annotations

import inspect
import textwrap
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .skills import Skill

_STRICT = ConfigDict(
    strict=True,
    frozen=True,
    extra="forbid",
    json_schema_serialization_defaults_required=True,
)


class FieldDeclaration(BaseModel):
    """One signature field as shown to people: its name, type name and description."""

    model_config = _STRICT

    name: str
    annotation: str
    description: str


class SignatureDeclaration(BaseModel):
    model_config = _STRICT

    name: str
    instructions: str
    inputs: tuple[FieldDeclaration, ...]
    outputs: tuple[FieldDeclaration, ...]


class ToolDeclaration(BaseModel):
    """A tool the agent can call. ``source_code`` is empty when it cannot be read."""

    model_config = _STRICT

    name: str
    description: str
    source_code: str


class SkillDeclaration(BaseModel):
    """An Agent Skill offered to the agent; ``files`` are its resource paths."""

    model_config = _STRICT

    name: str
    description: str
    instructions: str
    files: tuple[str, ...]


class PredictRLMSkillDeclaration(BaseModel):
    model_config = _STRICT

    name: str
    instructions: str
    packages: tuple[str, ...]
    modules: tuple[str, ...]
    tools: tuple[str, ...]


class PredictRLMDefaultModel(BaseModel):
    """No model was configured, so PredictRLM picks its own default."""

    model_config = _STRICT

    source: Literal["PredictRLM default"] = "PredictRLM default"


class PredictRLMConfiguredModel(BaseModel):
    """A model set on the step or as a workflow default."""

    model_config = _STRICT

    source: Literal["step override", "workflow default"]
    identity: JsonValue


PredictRLMModel = PredictRLMDefaultModel | PredictRLMConfiguredModel


class PredictRLMModels(BaseModel):
    model_config = _STRICT

    main: PredictRLMModel
    sub: PredictRLMModel


class PredictRLMDeclaration(BaseModel):
    """PredictRLM settings after workflow defaults and step overrides are applied.

    ``settings`` holds the effective constructor options that serialize to JSON,
    without credentials or callables. ``extra_options`` names the options
    Avalanche has no field for and passes unchanged to PredictRLM; their values
    appear in ``settings`` only under the same rules. ``skill_instructions``,
    ``skill_tools``, ``packages`` and ``modules`` are what the skills add, merged.
    """

    model_config = _STRICT

    kind: Literal["predict_rlm"] = "predict_rlm"
    settings: dict[str, JsonValue]
    extra_options: tuple[str, ...]
    models: PredictRLMModels
    skills: tuple[PredictRLMSkillDeclaration, ...]
    skill_instructions: str
    skill_tools: tuple[ToolDeclaration, ...]
    packages: tuple[str, ...]
    modules: tuple[str, ...]


class ClaudeAgentDeclaration(BaseModel):
    """What a Claude Agent SDK runtime contributes to a declaration.

    ``model`` is ``None`` when the step leaves the choice to the Claude CLI.
    """

    model_config = _STRICT

    kind: Literal["claude_agent"] = "claude_agent"
    model: str | None
    settings: dict[str, JsonValue]
    builtin_tools: tuple[str, ...]


# The OpenAI Agents SDK sandbox tools an OpenAIAgent step may turn on.
OpenAIBuiltinTool = Literal["filesystem", "shell"]


class OpenAIAgentDeclaration(BaseModel):
    """What an OpenAI Agents SDK runtime contributes to a declaration.

    ``model`` is ``None`` when the step leaves the choice to the SDK.
    """

    model_config = _STRICT

    kind: Literal["openai_agent"] = "openai_agent"
    model: str | None
    settings: dict[str, JsonValue]
    builtin_tools: tuple[OpenAIBuiltinTool, ...]


RuntimeDeclaration = Annotated[
    PredictRLMDeclaration | ClaudeAgentDeclaration | OpenAIAgentDeclaration,
    Field(discriminator="kind"),
]


class AgentDeclaration(BaseModel):
    """Everything discovery publishes about one agent step.

    ``tools`` and ``skills`` are the step's own; what a runtime adds, such as
    PredictRLM's skills and their tools, is in its ``runtime`` section.
    """

    model_config = _STRICT

    signature: SignatureDeclaration
    tools: tuple[ToolDeclaration, ...]
    skills: tuple[SkillDeclaration, ...]
    runtime: RuntimeDeclaration


def tool_declarations(
    tools: Mapping[str, Callable[..., object]],
) -> tuple[ToolDeclaration, ...]:
    """Describe tools by name, docstring and source; source is empty when unreadable."""
    declarations = []
    for name, tool in tools.items():
        try:
            source_code = textwrap.dedent(inspect.getsource(tool)).rstrip()
        except (OSError, TypeError):
            source_code = ""
        declarations.append(
            ToolDeclaration(
                name=name, description=inspect.getdoc(tool) or "", source_code=source_code
            )
        )
    return tuple(declarations)


def skill_declarations(skills: Sequence[Skill]) -> tuple[SkillDeclaration, ...]:
    """Describe Agent Skills by name, description, instructions and resource paths."""
    return tuple(
        SkillDeclaration(
            name=skill.name,
            description=skill.description,
            instructions=skill.instructions,
            files=tuple(sorted(skill.files)),
        )
        for skill in skills
    )
