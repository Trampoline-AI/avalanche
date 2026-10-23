"""The PredictRLM agent runtime."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Callable, ClassVar, Mapping, Self

import dspy
from dspy.primitives.code_interpreter import CodeInterpreter
from predict_rlm import (
    EventSink,
    ExecutionBackend,
    InputAdapter,
    OutputAdapter,
    RuntimeContribution,
    RuntimeHook,
    RuntimeHookEvent,
    SbxConfig,
    Skill,
    SubmitConfirmationContext,
)
from predict_rlm import PredictRLM as _PredictRLMPredictor
from predict_rlm.runtime import RuntimeModule
from predict_rlm.telemetry import TelemetryContext
from pydantic import BaseModel, ConfigDict, GetCoreSchemaHandler, PlainValidator, PrivateAttr
from pydantic_core import CoreSchema, core_schema

from ..._agent_evidence import AgentTraceFinishedEvent
from ...agent.steps import (
    _AGENT_RUNTIME_DEFAULTS,
    AgentStepExecutionError,
    _describe_signature,
)
from ...agent.contract import (
    AgentEventSink,
    AgentRequest,
    AgentRunOutcome,
    PythonOutputs,
    SignatureSpec,
    ToolSpec,
)
from ...agent.declaration import (
    PredictRLMConfiguredModel,
    PredictRLMDeclaration,
    PredictRLMDefaultModel,
    PredictRLMModel,
    PredictRLMModels,
    PredictRLMSkillDeclaration,
)
from ...agent.trace import RuntimeKind
from .events import _AGENT_INVOCATION_STATE, _AgentInvocationState, _AvalancheEvidenceSink
from .trace import (
    _emit_terminal_trace,
    _emit_trace_unavailable,
    _evidence_metadata,
    _model_display_name,
    trace_record,
)

_OMIT_METADATA = object()
_OMITTED_RUNTIME_KEYS = frozenset(
    {
        "events",
        "on_runtime_hook_event",
        "output_dir",
        "runtime_hooks",
        "submit_confirmation",
        "telemetry_context",
        "trace_export_path",
    }
)
_SECRET_KEY_PARTS = ("api_key", "auth", "credential", "password", "secret", "token")


def _skill_declaration(skill: Skill) -> PredictRLMSkillDeclaration:
    return PredictRLMSkillDeclaration(
        name=skill.name,
        instructions=skill.instructions,
        packages=tuple(skill.packages),
        modules=tuple(skill.modules),
        tools=tuple(skill.tools),
    )


def predict_rlm_declaration(
    workflow_defaults: Mapping[str, Any],
    step_overrides: Mapping[str, Any],
    *,
    skills: tuple[Skill, ...],
    signature_instructions: str,
) -> tuple[PredictRLMDeclaration, dict[str, Callable[..., Any]]]:
    """Describe a step's effective PredictRLM settings and return its skills' tools."""
    from predict_rlm.rlm_skills import merge_skills

    skill_instructions, packages, modules, skill_tools = merge_skills(list(skills))
    aggregated_instructions = signature_instructions
    if skill_instructions:
        aggregated_instructions += (
            "\n\n" if aggregated_instructions else ""
        ) + skill_instructions
    declaration = PredictRLMDeclaration(
        settings=_effective_runtime_metadata(workflow_defaults, step_overrides),
        models=_effective_model_metadata(workflow_defaults, step_overrides),
        skills=tuple(_skill_declaration(skill) for skill in skills),
        aggregated_static_instructions=aggregated_instructions,
        packages=tuple(packages),
        modules=tuple(modules),
    )
    return declaration, dict(skill_tools)


def _effective_runtime_metadata(
    workflow_defaults: Mapping[str, Any], step_overrides: Mapping[str, Any]
) -> dict[str, Any]:
    from predict_rlm import PredictRLM

    constructor = inspect.signature(PredictRLM.__init__)
    effective = {
        name: parameter.default
        for name, parameter in constructor.parameters.items()
        if name not in {"self", "signature", "skills", "tools"}
        and parameter.default is not inspect.Parameter.empty
    }
    effective.update(_AGENT_RUNTIME_DEFAULTS)
    effective.update(workflow_defaults)
    effective.update(step_overrides)
    serialized: dict[str, Any] = {}
    for name, value in effective.items():
        if name in _OMITTED_RUNTIME_KEYS or _is_sensitive_key(name):
            continue
        safe_value = _safe_runtime_value(value)
        if safe_value is not _OMIT_METADATA:
            serialized[name] = safe_value
    return serialized


def _effective_model_metadata(
    workflow_defaults: Mapping[str, Any], step_overrides: Mapping[str, Any]
) -> PredictRLMModels:
    """Describe only declaratively supported model sources; never probe DSPy globals."""
    models: dict[str, PredictRLMModel] = {}
    for runtime_key, label in (("lm", "main"), ("sub_lm", "sub")):
        if runtime_key in step_overrides:
            source = "step override"
            value = step_overrides[runtime_key]
        elif runtime_key in workflow_defaults:
            source = "workflow default"
            value = workflow_defaults[runtime_key]
        else:
            models[label] = PredictRLMDefaultModel()
            continue
        models[label] = PredictRLMConfiguredModel(
            source=source, identity=_strict_model_metadata_value(value, runtime_key)
        )
    return PredictRLMModels(main=models["main"], sub=models["sub"])


def _strict_model_metadata_value(value: Any, runtime_key: str, path: str = "") -> Any:
    """Serialize explicit model descriptors without silently omitting nested values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        _raise_unsupported_model_descriptor(value, runtime_key, path)
    if isinstance(value, Enum):
        return _strict_model_metadata_value(value.value, runtime_key, path)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _raise_unsupported_model_descriptor(key, runtime_key, path)
            if _is_sensitive_key(key):
                continue
            item_path = f"{path}.{key}" if path else key
            result[key] = _strict_model_metadata_value(item, runtime_key, item_path)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _strict_model_metadata_value(item, runtime_key, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]

    if inspect.isclass(value):
        return {"type": f"{value.__module__}.{value.__qualname__}"}

    value_type = type(value)
    module = value_type.__module__
    descriptor = {"type": f"{module}.{value_type.__qualname__}"}
    display_name = _model_display_name(value)
    if display_name is not None:
        descriptor["name"] = display_name
    elif module == "dspy" or module.startswith(("dspy.", "predict_rlm.")):
        instance_name = getattr(value, "model", None) or getattr(value, "name", None)
        if isinstance(instance_name, str):
            descriptor["name"] = instance_name
    return descriptor


def _raise_unsupported_model_descriptor(value: Any, runtime_key: str, path: str) -> None:
    value_type = type(value)
    location = f" at {path}" if path else ""
    raise TypeError(
        f"Unsupported {runtime_key} model descriptor{location}: "
        f"{value_type.__module__}.{value_type.__qualname__}; "
        "use a string, JSON-compatible descriptor, or a DSPy/PredictRLM model."
    )


def _safe_runtime_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _OMIT_METADATA
    if isinstance(value, Enum):
        return _safe_runtime_value(value.value)
    if isinstance(value, Path) or callable(value):
        return _OMIT_METADATA
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or _is_sensitive_key(key):
                continue
            safe_item = _safe_runtime_value(item)
            if safe_item is not _OMIT_METADATA:
                result[key] = safe_item
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            safe_item = _safe_runtime_value(item)
            if safe_item is not _OMIT_METADATA:
                result.append(safe_item)
        return result

    value_type = type(value)
    module = value_type.__module__
    if module == "dspy" or module.startswith(("dspy.", "predict_rlm.")):
        descriptor = {"type": f"{module}.{value_type.__qualname__}"}
        instance_name = getattr(value, "model", None) or getattr(value, "name", None)
        if isinstance(instance_name, str):
            descriptor["name"] = instance_name
        return descriptor
    return _OMIT_METADATA


def _is_sensitive_key(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def _build_predictor(
    signature: type[dspy.Signature],
    *,
    skills: tuple[Any, ...],
    tools: tuple[Callable[..., Any], ...],
    **runtime_kwargs: Any,
) -> Any:
    """Build the agent predictor behind a testable, lazy import seam."""
    from predict_rlm import PredictRLM

    configured_events = tuple(runtime_kwargs.pop("events", ()))
    return PredictRLM(
        signature,
        skills=list(skills),
        tools=list(tools),
        events=(*configured_events, _AvalancheEvidenceSink()),
        **runtime_kwargs,
    )


@dataclass(frozen=True)
class _IsInstance:
    """Check a PredictRLM value with ``isinstance`` alone.

    Several PredictRLM option types cannot be described as Pydantic schemas;
    their own constructors already validated them.
    """

    cls: type

    def __get_pydantic_core_schema__(
        self, source: type, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(self.cls)


_Skill = Annotated[Skill, _IsInstance(Skill)]
_SbxConfig = Annotated[SbxConfig, _IsInstance(SbxConfig)]
_TelemetryContext = Annotated[TelemetryContext, _IsInstance(TelemetryContext)]
_RuntimeHook = Annotated[RuntimeHook, _IsInstance(RuntimeHook)]
_RuntimeContribution = Annotated[RuntimeContribution, _IsInstance(RuntimeContribution)]


def _check_sbx_pool(value: object) -> object:
    # SbxPool needs predict-rlm's optional ``sbx`` extra; import it only when one is passed.
    from predict_rlm import SbxPool

    if not isinstance(value, SbxPool):
        raise ValueError(f"sbx_pool must be a predict_rlm.SbxPool, got {type(value).__name__}")
    return value


_SbxPool = Annotated[object, PlainValidator(_check_sbx_pool)]


@dataclass(frozen=True)
class PredictRLMCall:
    """A finished PredictRLM call and the v1 terminal event already emitted for it."""

    prediction: dspy.Prediction
    terminal_event: AgentTraceFinishedEvent


class PredictRLM(BaseModel):
    """Run agent steps with PredictRLM.

    Fields mirror ``predict_rlm.PredictRLM`` except the signature and tools,
    which come from each request. Nothing is built until the first invocation.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", arbitrary_types_allowed=True
    )

    kind: ClassVar[RuntimeKind] = "predict_rlm"

    lm: dspy.BaseLM | str | None = None
    sub_lm: dspy.BaseLM | str | None = None
    max_iterations: int = 30
    max_llm_calls: int = 50
    max_output_chars: int = 50_000
    verbose: bool = True
    interpreter: CodeInterpreter | None = None
    sandbox_backend: str | None = None
    sbx_config: _SbxConfig | None = None
    sbx_pool: _SbxPool | None = None
    allowed_domains: Sequence[str] | None = None
    skills: Sequence[_Skill] | None = None
    debug: bool = False
    output_dir: str | Path | None = None
    telemetry_context: _TelemetryContext | None = None
    submit_confirmation: Callable[[SubmitConfirmationContext], str | None] | None = None
    trace_export_path: str | Path | None = None
    runtime_hooks: Sequence[_RuntimeHook] | None = None
    on_runtime_hook_event: Callable[[RuntimeHookEvent], object] | None = None
    model_execution_timeout: bool = False
    adapters: Sequence[InputAdapter | OutputAdapter] = ()
    execution: ExecutionBackend | None = None
    modules: Sequence[RuntimeModule | _RuntimeContribution] = ()
    events: Sequence[EventSink] = ()
    run_export_root: str | Path | None = None

    # Built predictors, keyed by the signature and tools they were built for.
    _predictors: list[tuple[SignatureSpec, tuple[ToolSpec, ...], _PredictRLMPredictor]] = (
        PrivateAttr(default_factory=list)
    )

    def __getstate__(self) -> dict[str, object]:
        state = super().__getstate__()
        return {**state, "__pydantic_private__": {"_predictors": []}}

    async def invoke(self, request: AgentRequest, events: AgentEventSink) -> AgentRunOutcome:
        call = await self.predict(request, events)
        prediction = call.prediction
        return AgentRunOutcome(
            outputs=PythonOutputs(
                values={
                    name: prediction[name]
                    for name in request.signature.output_model.model_fields
                }
            ),
            trace=trace_record(
                prediction.trace,
                prediction.evidence,
                model=_model_display_name(self.lm),
                sub_model=_model_display_name(self.sub_lm),
            ),
        )

    async def predict(
        self, request: AgentRequest, events: AgentEventSink | None = None
    ) -> PredictRLMCall:
        """Run PredictRLM and return its native prediction.

        Agent steps still return this prediction until they return an
        ``AgentResult``; new callers should use ``invoke``.
        """
        predictor = self._predictor_for(request)
        inputs = {
            name: getattr(request.inputs, name)
            for name in request.signature.input_model.model_fields
        }
        runtime_kwargs = {"lm": self.lm, "sub_lm": self.sub_lm}

        from predict_rlm.trace import extract_trace_from_exc

        state = _AgentInvocationState(request.invocation_id, events)
        invocation_token = _AGENT_INVOCATION_STATE.set(state)
        try:
            try:
                prediction: dspy.Prediction = await predictor.acall(**inputs)
            except asyncio.CancelledError as exc:
                try:
                    trace = extract_trace_from_exc(exc)
                    evidence = _evidence_metadata(exc.evidence)
                    if trace is None:
                        _emit_trace_unavailable(
                            exc, invocation_id=state.invocation_id, evidence=evidence
                        )
                    else:
                        _emit_terminal_trace(
                            trace,
                            invocation_id=state.invocation_id,
                            evidence=evidence,
                            runtime_kwargs=runtime_kwargs,
                        )
                except Exception as evidence_error:
                    exc.add_note(
                        f"Failed to retain agent cancellation detail: {evidence_error}"
                    )
                raise
            except Exception as exc:
                if state.listener_base_exception is not None:
                    raise state.listener_base_exception
                trace = extract_trace_from_exc(exc)
                evidence = _evidence_metadata(exc.evidence)
                if trace is None:
                    _emit_trace_unavailable(
                        exc, invocation_id=state.invocation_id, evidence=evidence
                    )
                else:
                    _emit_terminal_trace(
                        trace,
                        invocation_id=state.invocation_id,
                        evidence=evidence,
                        runtime_kwargs=runtime_kwargs,
                    )
                input_types = {name: type(value).__name__ for name, value in inputs.items()}
                raise AgentStepExecutionError(
                    f"agent step {request.step_name!r} failed calling "
                    f"{_describe_signature(request.signature.dspy_signature)}: {exc}. "
                    f"input types: {input_types}."
                ) from exc

            terminal_event = _emit_terminal_trace(
                prediction.trace,
                invocation_id=state.invocation_id,
                evidence=_evidence_metadata(prediction.evidence),
                runtime_kwargs=runtime_kwargs,
            )
            return PredictRLMCall(prediction=prediction, terminal_event=terminal_event)
        finally:
            _AGENT_INVOCATION_STATE.reset(invocation_token)

    def _predictor_for(self, request: AgentRequest) -> _PredictRLMPredictor:
        for signature, tools, predictor in self._predictors:
            if signature is request.signature and tools == request.tools:
                return predictor
        options = {name: getattr(self, name) for name in self.model_fields_set - {"skills"}}
        predictor = _build_predictor(
            request.signature.dspy_signature,
            skills=tuple(self.skills or ()),
            tools=tuple(tool.function for tool in request.tools),
            **options,
        )
        self._predictors.append((request.signature, request.tools, predictor))
        return predictor

    @classmethod
    def from_runtime_kwargs(
        cls, runtime_kwargs: Mapping[str, object], *, skills: Sequence[Skill]
    ) -> Self:
        """The descriptor for today's ``lm=``/``sub_lm=``/... agent-step arguments."""
        return cls.model_validate({**runtime_kwargs, "skills": tuple(skills)})
