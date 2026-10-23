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
from collections.abc import Callable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

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


# A discriminated union on ``kind`` once a second runtime declares itself.
RuntimeDeclaration = PredictRLMDeclaration


class AgentDeclaration(BaseModel):
    """Everything discovery publishes about one agent step.

    ``tools`` are the step's own tools; tools a runtime adds, such as PredictRLM
    skill tools, are in its ``runtime`` section.
    """

    model_config = _STRICT

    signature: SignatureDeclaration
    tools: tuple[ToolDeclaration, ...]
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
