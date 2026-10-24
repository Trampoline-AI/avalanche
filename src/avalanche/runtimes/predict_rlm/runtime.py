"""The PredictRLM agent runtime."""

from __future__ import annotations

import asyncio
import inspect
import math
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Callable, ClassVar, Mapping

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
from ...agent.steps import AgentStepExecutionError, _describe_signature
from ...agent.trace import RuntimeKind
from . import files
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


def _effective_runtime_metadata(runtime: PredictRLM) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for name in PredictRLM.model_fields:
        if name == "skills" or name in _OMITTED_RUNTIME_KEYS or _is_sensitive_key(name):
            continue
        safe_value = _safe_runtime_value(getattr(runtime, name))
        if safe_value is not _OMIT_METADATA:
            serialized[name] = safe_value
    return serialized


def _effective_model_metadata(
    step: PredictRLM, workflow_default: PredictRLM | None
) -> PredictRLMModels:
    """Describe where each model came from; never probe DSPy globals."""
    models: dict[str, PredictRLMModel] = {}
    for runtime_key, label in (("lm", "main"), ("sub_lm", "sub")):
        if runtime_key in step.model_fields_set:
            source = "step override"
            value = getattr(step, runtime_key)
        elif workflow_default is not None and runtime_key in workflow_default.model_fields_set:
            source = "workflow default"
            value = getattr(workflow_default, runtime_key)
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
    # Avalanche runs PredictRLM quietly unless asked; the constructor defaults to True.
    verbose: bool = False
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
        predictor = self._predictor_for(request)
        signature = request.signature.dspy_signature
        input_files = files.file_fields(signature, signature.input_fields)
        output_files = files.file_fields(signature, signature.output_fields)
        inputs = {
            name: getattr(request.inputs, name)
            for name in request.signature.input_model.model_fields
        }
        with tempfile.TemporaryDirectory(prefix="avalanche-predict-rlm-") as directory:
            prediction = await self._run(
                predictor,
                request,
                files.host_inputs(inputs, input_files, Path(directory)),
                input_types={name: type(value).__name__ for name, value in inputs.items()},
                events=events,
            )
            output_names = request.signature.output_model.model_fields
            # A missing field is left out, so output validation reports it.
            values = files.avalanche_outputs(
                {name: prediction[name] for name in output_names if name in prediction},
                output_files,
            )
        return AgentRunOutcome(
            outputs=PythonOutputs(values=values),
            trace=trace_record(
                prediction.trace,
                prediction.evidence,
                model=_model_display_name(self.lm),
                sub_model=_model_display_name(self.sub_lm),
            ),
        )

    async def _run(
        self,
        predictor: _PredictRLMPredictor,
        request: AgentRequest,
        inputs: Mapping[str, object],
        *,
        input_types: Mapping[str, str],
        events: AgentEventSink,
    ) -> dspy.Prediction:
        """Run PredictRLM, sending its v1 terminal trace event on every outcome."""
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
                raise AgentStepExecutionError(
                    f"agent step {request.step_name!r} failed calling "
                    f"{_describe_signature(request.signature.dspy_signature)}: {exc}. "
                    f"input types: {dict(input_types)}."
                ) from exc

            _emit_terminal_trace(
                prediction.trace,
                invocation_id=state.invocation_id,
                evidence=_evidence_metadata(prediction.evidence),
                runtime_kwargs=runtime_kwargs,
            )
            return prediction
        finally:
            _AGENT_INVOCATION_STATE.reset(invocation_token)

    def _predictor_for(self, request: AgentRequest) -> _PredictRLMPredictor:
        for signature, tools, predictor in self._predictors:
            if signature is request.signature and tools == request.tools:
                return predictor
        # ``verbose`` always goes through: Avalanche's default differs from PredictRLM's.
        passed = (self.model_fields_set | {"verbose"}) - {"skills"}
        options = {name: getattr(self, name) for name in passed}
        predictor = _build_predictor(
            files.predictor_signature(request.signature.dspy_signature),
            skills=tuple(self.skills or ()),
            tools=tuple(tool.function for tool in request.tools),
            **options,
        )
        self._predictors.append((request.signature, request.tools, predictor))
        return predictor

    def merged_over(self, base: PredictRLM) -> PredictRLM:
        """This runtime with the fields it leaves unset taken from ``base``."""
        fields = {name: getattr(base, name) for name in base.model_fields_set}
        fields.update({name: getattr(self, name) for name in self.model_fields_set})
        return PredictRLM.model_validate(fields)

    def declaration(self, *, workflow_default: PredictRLM | None) -> PredictRLMDeclaration:
        """Describe this step runtime merged over the workflow default, statically."""
        from predict_rlm.rlm_skills import merge_skills

        from ...agent.declaration import tool_declarations

        effective = self if workflow_default is None else self.merged_over(workflow_default)
        skills = tuple(effective.skills or ())
        skill_instructions, packages, modules, skill_tools = merge_skills(list(skills))
        return PredictRLMDeclaration(
            settings=_effective_runtime_metadata(effective),
            models=_effective_model_metadata(self, workflow_default),
            skills=tuple(_skill_declaration(skill) for skill in skills),
            skill_instructions=skill_instructions,
            skill_tools=tool_declarations(skill_tools),
            packages=tuple(packages),
            modules=tuple(modules),
        )
