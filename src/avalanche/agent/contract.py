"""Runtime-neutral contract between agent steps and agent runtimes.

An agent step describes what it wants as an ``AgentRequest``; a runtime runs it
and returns an ``AgentRunOutcome`` with raw outputs and a trace. Avalanche then
validates the outputs against the step's signature, so every runtime shares one
validation path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated, ClassVar, Literal, Protocol, Self, runtime_checkable

import dspy
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    create_model,
    model_validator,
)
from pydantic.fields import FieldInfo

from .declaration import RuntimeDeclaration
from .live import AgentEvent, RunStarted, ToolFinished, ToolStarted, TurnFinished
from .native import NativeEvent
from .trace import AgentTrace, AgentTraceHeader, AgentTraceStatus, RuntimeKind

# Serialized bodies always carry defaulted fields such as ``kind``; their
# exported JSON Schema says so.
_STRICT = ConfigDict(
    strict=True,
    frozen=True,
    extra="forbid",
    json_schema_serialization_defaults_required=True,
)
_LIVE = ConfigDict(strict=True, frozen=True, extra="forbid", arbitrary_types_allowed=True)

# DSPy field metadata that carries no meaning beyond the description: the role
# marker, the deprecated no-op prefix, a placeholder for untyped string fields,
# and a rendering of constraints that ``FieldInfo.metadata`` already holds.
_DSPY_FIELD_KEYS = frozenset(
    {"__dspy_field_type", "desc", "prefix", "IS_TYPE_UNDEFINED", "constraints"}
)


class FieldSpec(BaseModel):
    """One signature field as runtimes and viewers see it."""

    model_config = _STRICT

    name: str
    description: str | None
    json_schema: dict[str, JsonValue]


class SignatureSpec(BaseModel):
    """A step's typed contract; the only place in the agent layer that reads DSPy."""

    model_config = _LIVE

    name: str
    instructions: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    # The source signature, for runtimes that are themselves built on DSPy.
    dspy_signature: type[dspy.Signature]

    @classmethod
    def from_dspy(cls, signature: type[dspy.Signature]) -> Self:
        if not signature.output_fields:
            raise TypeError(f"agent signature {signature.__name__} declares no output fields")
        name = signature.__name__
        return cls(
            name=name,
            instructions=signature.instructions,
            input_model=_fields_model(f"{name}Inputs", signature, signature.input_fields),
            output_model=_fields_model(f"{name}Outputs", signature, signature.output_fields),
            dspy_signature=signature,
        )

    @property
    def inputs(self) -> tuple[FieldSpec, ...]:
        return _field_specs(self.input_model)

    @property
    def outputs(self) -> tuple[FieldSpec, ...]:
        return _field_specs(self.output_model)

    def validate_outputs(self, outputs: AgentOutputs) -> BaseModel:
        """Validate raw runtime outputs into an instance of ``output_model``."""
        match outputs:
            case PythonOutputs(values=values):
                return self.output_model.model_validate(dict(values), strict=True)
            case JsonOutputs(text=text):
                return self.output_model.model_validate_json(text, strict=True)


def _fields_model(
    model_name: str,
    signature: type[dspy.Signature],
    fields: Mapping[str, FieldInfo],
) -> type[BaseModel]:
    definitions: dict[str, tuple[object, FieldInfo]] = {}
    for field_name, field in fields.items():
        extra = field.json_schema_extra
        if not isinstance(extra, dict):
            raise TypeError(
                f"agent signature {signature.__name__} field {field_name!r} "
                "is not a DSPy InputField or OutputField"
            )
        unsupported = sorted(set(extra) - _DSPY_FIELD_KEYS)
        if unsupported:
            raise TypeError(
                f"agent signature {signature.__name__} field {field_name!r} uses "
                f"unsupported DSPy field options {unsupported}"
            )
        description = extra.get("desc")
        if not isinstance(description, str) or description == f"${{{field_name}}}":
            description = None
        # Agent signature fields are always required; a DSPy default is not applied.
        definitions[field_name] = (_constrained(field), Field(description=description))
    return create_model(model_name, __config__=_LIVE, **definitions)


def _constrained(field: FieldInfo) -> object:
    """The field's annotation with its constraints, such as ``ge=0``, attached."""
    if field.metadata:
        return Annotated[field.annotation, *field.metadata]
    return field.annotation


def _field_specs(model: type[BaseModel]) -> tuple[FieldSpec, ...]:
    return tuple(
        FieldSpec(
            name=name,
            description=field.description,
            json_schema=TypeAdapter(_constrained(field)).json_schema(),
        )
        for name, field in model.model_fields.items()
    )


class PythonOutputs(BaseModel):
    """Outputs returned as Python values; validated in strict Python mode.

    ``values`` is the untyped boundary of a Python-native runtime and is
    validated into the signature's output model before anything reads it.
    """

    model_config = _LIVE

    kind: Literal["python"] = "python"
    values: Mapping[str, object]


class JsonOutputs(BaseModel):
    """Outputs returned as a JSON document; validated in strict JSON mode."""

    model_config = _STRICT

    kind: Literal["json"] = "json"
    text: str


AgentOutputs = Annotated[PythonOutputs | JsonOutputs, Field(discriminator="kind")]


class ToolSpec(BaseModel):
    """A Python function offered to the agent as a tool."""

    model_config = _LIVE

    name: str
    description: str | None
    function: Callable[..., object]


class AgentRequest(BaseModel):
    """Everything a runtime needs for one invocation.

    ``inputs`` is an instance of the signature's input model. ``ava.File`` and
    ``ava.Workspace`` values pass through unchanged; each runtime decides how to
    hand them to its agent.
    """

    model_config = _LIVE

    invocation_id: str
    step_name: str
    signature: SignatureSpec
    inputs: BaseModel
    tools: tuple[ToolSpec, ...]

    @model_validator(mode="after")
    def _check_inputs(self) -> Self:
        if type(self.inputs) is not self.signature.input_model:
            raise ValueError(
                f"agent request inputs must be a {self.signature.input_model.__name__}, "
                f"got {type(self.inputs).__name__}"
            )
        return self


class AgentRunOutcome(BaseModel):
    """What a runtime returns: raw outputs plus the invocation's trace."""

    model_config = _LIVE

    outputs: AgentOutputs
    trace: AgentTrace


class AgentResult(BaseModel):
    """A finished invocation: validated outputs plus the trace."""

    model_config = _LIVE

    outputs: BaseModel
    trace: AgentTrace


class AgentEventSink(Protocol):
    """Receives an invocation's live events and its one final event.

    Avalanche assigns sequence numbers and timestamps. A runtime sends each live
    event as its native payload, the common event, or both, and finishes with
    exactly one ``finished`` or ``unavailable`` call, also when the run fails or
    is cancelled.
    """

    async def emit(self, native: NativeEvent | None, common: AgentEvent | None) -> None: ...

    def finished(
        self, header: AgentTraceHeader, *, outputs: AgentOutputs | None, complete: bool
    ) -> None:
        """Send the trace header and the run's raw outputs, ``None`` when it has none.

        The turns already travelled as live events; Avalanche keeps the outputs
        within the trace size limit.
        """
        ...

    def unavailable(
        self,
        *,
        status: AgentTraceStatus,
        error: str,
        external_run_id: str | None,
        complete: bool,
    ) -> None:
        """Report that the run ended without a trace the runtime could export."""
        ...


@runtime_checkable
class AgentRuntime(Protocol):
    """An agent runtime that can run an ``AgentRequest``.

    Runtimes are frozen descriptors: building one starts nothing, and it can
    be pickled to a worker.
    """

    kind: ClassVar[RuntimeKind]

    def merged_over(self, base: Self) -> Self:
        """This runtime with the fields it leaves unset taken from ``base``."""
        ...

    def declaration(self, *, workflow_default: Self | None) -> RuntimeDeclaration:
        """Describe this step runtime merged over the workflow default, statically."""
        ...

    async def invoke(
        self, request: AgentRequest, events: AgentEventSink
    ) -> AgentRunOutcome: ...


__all__ = [
    "AgentEvent",
    "AgentEventSink",
    "AgentOutputs",
    "AgentRequest",
    "AgentResult",
    "AgentRunOutcome",
    "AgentRuntime",
    "FieldSpec",
    "JsonOutputs",
    "PythonOutputs",
    "RunStarted",
    "SignatureSpec",
    "ToolFinished",
    "ToolSpec",
    "ToolStarted",
    "TurnFinished",
]
