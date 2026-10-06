"""Bodyful and inline agent workflow nodes."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import textwrap
import types
import uuid
from contextvars import ContextVar
from enum import Enum
from functools import update_wrapper
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    DefaultDict,
    Generic,
    Mapping,
    Never,
    Sequence,
    TypeAlias,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

import dspy
from dspy import Prediction
from pydantic import BaseModel, TypeAdapter

from .._agent_evidence import (
    AGENT_ERROR_CHARACTER_LIMIT,
    AgentInvocationId,
    AgentTraceFinishedEvent,
    AgentTraceUnavailableEvent,
    emit_agent_evidence,
)
from ..dag import Node, NodeFuture, NodeType, _workflow_context
from ..evaluations import Evaluations
from ..step_interface import (
    decoration_namespace,
    resolve_step_signature,
    step_interface_from_signature,
)
from .config import UNSET, validate_runtime_kwargs
from .signature import resolve_signature

if TYPE_CHECKING:
    from predict_rlm import IterationStep, RunEvent, RunEvidence, RunTrace

    from .._agent_trace import AgentEvidenceMetadata

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class AgentStepError(RuntimeError):
    """An invalid agent-step declaration or agent-call boundary."""


class AgentStepExecutionError(RuntimeError):
    """An agent invocation failed inside an agent step."""


_WORKFLOW_AGENT_DEFAULTS: ContextVar[Mapping[str, Any]] = ContextVar(
    "avalanche_agent_workflow_defaults", default={}
)

_AGENT_RUNTIME_DEFAULTS: Mapping[str, bool] = types.MappingProxyType({"verbose": False})


class _AgentInvocationState:
    """Task-local evidence state for one agent invocation."""

    def __init__(self, invocation_id: AgentInvocationId) -> None:
        self.invocation_id = invocation_id
        self.listener_base_exception: BaseException | None = None


_AGENT_INVOCATION_STATE: ContextVar[_AgentInvocationState | None] = ContextVar(
    "avalanche_agent_invocation_state", default=None
)


class _AvalancheEvidenceSink:
    """Project PredictRLM evidence under the bridge's explicit error policy."""

    strict = True

    async def emit(self, event: RunEvent) -> None:
        state = _current_invocation_state()
        projected = _project_evidence_event(
            event,
            invocation_id=state.invocation_id,
        )
        _emit_sink_evidence(projected, state=state)

    async def flush(self, run_id: str) -> None:
        return None

    async def close(self, run_id: str, terminal_event: RunEvent | None = None) -> None:
        if terminal_event is not None:
            state = _current_invocation_state()
            projected = _project_evidence_event(
                terminal_event,
                invocation_id=state.invocation_id,
            )
            _emit_sink_evidence(projected, state=state)


def _current_invocation_state() -> _AgentInvocationState:
    state = _AGENT_INVOCATION_STATE.get()
    if state is None:
        raise RuntimeError("agent evidence emitted outside an agent invocation")
    return state


def _emit_sink_evidence(
    event: dict[str, Any],
    *,
    state: _AgentInvocationState,
) -> None:
    try:
        emit_agent_evidence(event)
    except BaseException as exc:
        if not isinstance(exc, Exception) and state.listener_base_exception is None:
            state.listener_base_exception = exc
        raise


JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
_MAX_EVIDENCE_VALUE_BYTES = 4 * 1024 * 1024
_MAX_EVIDENCE_COLLECTION_ITEMS = 10_000
_MAX_EVIDENCE_DEPTH = 32


def _unavailable_value(reason: str) -> dict[str, JsonValue]:
    return {"kind": "unavailable", "reason": reason}


def _project_agent_value(value: object, *, depth: int = 0) -> JsonValue:
    if depth > _MAX_EVIDENCE_DEPTH:
        return _unavailable_value("maximum nesting depth exceeded")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _unavailable_value("non-finite number")

    try:
        import predict_rlm
    except ImportError:
        predict_rlm = None
    if predict_rlm is not None and isinstance(value, predict_rlm.File):
        path = value.path
        if isinstance(path, str) and path:
            return {"kind": "predict_rlm_file", "path": path}
        return _unavailable_value("PredictRLM file has no host path")

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        if len(value) > _MAX_EVIDENCE_COLLECTION_ITEMS:
            return _unavailable_value("mapping exceeds item limit")
        projected: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                return _unavailable_value("mapping keys must be strings")
            projected[key] = _project_agent_value(item, depth=depth + 1)
        return projected
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_EVIDENCE_COLLECTION_ITEMS:
            return _unavailable_value("sequence exceeds item limit")
        return [_project_agent_value(item, depth=depth + 1) for item in value]
    return _unavailable_value(f"unsupported value type: {type(value).__name__}")


def _bounded_agent_error(error: str) -> str:
    if len(error) <= AGENT_ERROR_CHARACTER_LIMIT:
        return error
    marker = "\n[unavailable: remaining error text exceeds character limit]"
    return error[: AGENT_ERROR_CHARACTER_LIMIT - len(marker)] + marker


def _bound_live_detail(projected: dict[str, JsonValue]) -> None:
    """Apply inspection policy once, after selecting an event's public fields."""
    if "error" in projected:
        error = projected["error"]
        if not isinstance(error, str):
            raise TypeError("live agent error must be a string")
        projected["error"] = _bounded_agent_error(error)

    def size(value: JsonValue) -> int:
        return len(json.dumps(value, separators=(",", ":")).encode())

    remaining = size(projected) - _MAX_EVIDENCE_VALUE_BYTES
    if remaining <= 0:
        return
    marker = _unavailable_value("live event detail exceeds byte limit")
    text_marker = "[unavailable: live event detail exceeds byte limit]"
    candidates: list[tuple[int, str, JsonValue]] = []
    for key, value in projected.items():
        # Correlation IDs and measured state are not inspection payloads.
        if key in {"call_id", "status", "error_type", "error"}:
            continue
        replacement: JsonValue
        if key in {"inputs", "outputs", "output"}:
            replacement = marker
        elif isinstance(value, str):
            replacement = text_marker
        elif isinstance(value, list):
            replacement = [text_marker]
        else:
            continue
        saving = size(value) - size(replacement)
        if saving > 0:
            candidates.append((saving, key, replacement))
    for saving, key, replacement in sorted(candidates, key=lambda item: item[0], reverse=True):
        projected[key] = replacement
        remaining -= saving
        if remaining <= 0:
            return
    raise ValueError("live event identity metadata exceeds byte limit")


def _bounded_iteration_step(
    step: IterationStep,
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    """Keep an SDK-shaped step within the aggregate inspection-data budget."""

    def fields(model: BaseModel, *, exclude: set[str]) -> dict[str, JsonValue]:
        value = _project_agent_value(model.model_dump(mode="json", exclude=exclude))
        assert isinstance(value, dict)
        return value

    projected = fields(step, exclude={"tool_calls", "predict_calls"})
    tools: list[JsonValue] = []
    groups: list[JsonValue] = []
    projected["tool_calls"] = tools
    projected["predict_calls"] = groups
    for tool in step.tool_calls:
        detail = fields(tool, exclude={"args", "kwargs", "result"})
        args = _project_agent_value(tool.args)
        detail["args"] = args if isinstance(args, list) else [args]
        detail["kwargs"] = _project_agent_value(tool.kwargs)
        detail["result"] = _project_agent_value(tool.result)
        tools.append(detail)
    for group in step.predict_calls:
        detail = fields(group, exclude={"calls"})
        calls: list[JsonValue] = []
        detail["calls"] = calls
        for call in group.calls:
            call_detail = fields(call, exclude={"input", "output"})
            call_detail["input"] = _project_agent_value(call.input)
            call_detail["output"] = _project_agent_value(call.output)
            calls.append(call_detail)
        groups.append(detail)

    def size(value: JsonValue) -> int:
        return len(json.dumps(value, separators=(",", ":")).encode())

    remaining = size(projected) - _MAX_EVIDENCE_VALUE_BYTES
    if remaining <= 0:
        return projected, {}

    # Omit the largest inspection fields first, leaving small detail and SDK
    # identity/timing/usage intact. Replacements retain each field's SDK type.
    marker = _unavailable_value("iteration detail exceeds byte limit")
    text_marker = "[unavailable: iteration detail exceeds byte limit]"
    candidates: list[tuple[int, dict[str, JsonValue], str, JsonValue]] = []

    def collect(value: JsonValue) -> None:
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                replacement: JsonValue
                if isinstance(item, str):
                    replacement = text_marker
                elif key in {"input", "output", "kwargs", "result"}:
                    replacement = marker
                elif key == "args":
                    replacement = [marker]
                else:
                    collect(item)
                    continue
                saving = size(item) - size(replacement)
                if saving > 0:
                    candidates.append((saving, value, key, replacement))

    collect(projected)
    for saving, container, key, replacement in sorted(
        candidates, key=lambda candidate: candidate[0], reverse=True
    ):
        container[key] = replacement
        remaining -= saving
        if remaining <= 0:
            return projected, {}

    # Even call identities can exceed the fixed budget. Keep a prefix and
    # expose omitted counts both on the event and in SDK-only inspection.
    reasoning = projected["reasoning"]
    assert isinstance(reasoning, str)

    def omission_note(tool_count: int, predict_count: int, group_count: int) -> str:
        return (
            "\n[unavailable: iteration detail exceeds byte limit; omitted "
            f"{tool_count} tool calls, {predict_count} predict calls, "
            f"and {group_count} predict groups]"
        )

    # Reserve the longest possible count note before trimming call collections.
    reserved_reasoning = reasoning + omission_note(
        len(step.tool_calls),
        sum(len(group.calls) for group in step.predict_calls),
        len(step.predict_calls),
    )
    projected["reasoning"] = reserved_reasoning
    remaining += size(reserved_reasoning) - size(reasoning)
    omitted_tools = 0
    omitted_predicts = 0
    omitted_groups = 0
    while tools and remaining > 0:
        remaining -= size(tools.pop()) + (1 if tools else 0)
        omitted_tools += 1
    for group_detail in reversed(groups):
        assert isinstance(group_detail, dict)
        group_calls = group_detail["calls"]
        assert isinstance(group_calls, list)
        while group_calls and remaining > 0:
            remaining -= size(group_calls.pop()) + (1 if group_calls else 0)
            omitted_predicts += 1
        if remaining <= 0:
            break
    while groups and remaining > 0:
        remaining -= size(groups.pop()) + (1 if groups else 0)
        omitted_groups += 1
    projected["reasoning"] = reasoning + omission_note(
        omitted_tools, omitted_predicts, omitted_groups
    )
    if size(projected) > _MAX_EVIDENCE_VALUE_BYTES:
        # Required numeric SDK metadata cannot be replaced by an omission
        # marker without inventing measurements or breaking the SDK schema.
        raise ValueError("iteration inspection metadata exceeds byte limit")
    return projected, {
        "reason": "iteration detail exceeds byte limit",
        "tool_count": omitted_tools,
        "predict_count": omitted_predicts,
        "predict_group_count": omitted_groups,
    }


def _project_evidence_event(
    event: RunEvent,
    *,
    invocation_id: AgentInvocationId,
) -> dict[str, JsonValue]:
    event_kind = event.kind.value
    data = event.data
    projected: dict[str, JsonValue] = {}

    if event_kind == "run.started":
        inputs = data["inputs"]
        projected = {
            "input_fields": sorted(inputs),
            "inputs": _project_agent_value(inputs),
        }
    elif event_kind == "iteration.recorded":
        from predict_rlm import IterationStep

        step = IterationStep.model_validate(data["step"], strict=True)
        bounded_step, omissions = _bounded_iteration_step(step)
        projected = {
            "iteration": step.iteration,
            "duration_ms": step.duration_ms,
            "error": step.error,
            "tool_count": len(step.tool_calls),
            "predict_count": sum(len(group.calls) for group in step.predict_calls),
            "step": bounded_step,
        }
        if omissions:
            projected["omissions"] = omissions
    elif event_kind == "predict.started":
        projected = {
            key: data[key] for key in ("call_id", "signature", "instructions", "model")
        }
    elif event_kind == "predict.finished":
        projected = {"call_id": data["call_id"]}
        if "error" in data:
            projected["error"] = data["error"]
    elif event_kind in {"tool.started", "tool.finished"}:
        projected = {"call_id": data["call_id"], "name": data["name"]}
        if "error" in data:
            projected["error"] = data["error"]
    elif event_kind == "code.generated":
        projected = {"iteration": data["iteration"], "code": data["code"]}
    elif event_kind == "code.executed":
        projected = {"iteration": data["iteration"]}
        for key in ("output", "error"):
            if key in data:
                projected[key] = data[key]
    elif event_kind == "run.succeeded":
        projected = {
            "status": data["status"],
            "outputs": _project_agent_value(data["outputs"]),
        }
    elif event_kind in {"run.failed", "run.cancelled"}:
        projected = {"error_type": data["error_type"], "error": data["error"]}
    if event_kind != "iteration.recorded":
        _bound_live_detail(projected)

    return {
        "kind": "evidence",
        "invocation_id": invocation_id,
        "sequence": event.sequence,
        "event_kind": event_kind,
        "timestamp_ns": event.timestamp_ns,
        "data": projected,
    }


def _evidence_metadata(evidence: RunEvidence) -> AgentEvidenceMetadata:
    from .._agent_trace import AgentEvidenceMetadata

    return AgentEvidenceMetadata(
        run_id=evidence.run_id,
        complete=evidence.complete,
        terminal_outcome=evidence.terminal_outcome,
    )


def _emit_terminal_trace(
    trace: RunTrace,
    *,
    invocation_id: AgentInvocationId,
    evidence: AgentEvidenceMetadata,
) -> AgentTraceFinishedEvent:
    # Iterations already crossed the bounded event channel. Do not re-send
    # original payloads that the operator would discard after validation.
    parsed = json.loads(trace.model_copy(update={"steps": []}).to_exportable_json())
    event: AgentTraceFinishedEvent = {
        "kind": "trace_finished",
        "invocation_id": invocation_id,
        "trace": parsed,
        "evidence": evidence.model_dump(mode="json"),
    }
    emit_agent_evidence(event)
    return event


def _emit_trace_unavailable(
    error: BaseException,
    *,
    invocation_id: AgentInvocationId,
    evidence: AgentEvidenceMetadata,
) -> AgentTraceUnavailableEvent:
    event: AgentTraceUnavailableEvent = {
        "kind": "trace_unavailable",
        "invocation_id": invocation_id,
        "error": _bounded_agent_error(str(error)),
        "evidence": evidence.model_dump(mode="json"),
    }
    emit_agent_evidence(event)
    return event


class Agent:
    """Injected callable that executes its step's explicit Signature."""

    def __init__(
        self,
        *,
        signature: type[dspy.Signature],
        step_name: str,
        runtime_kwargs: Mapping[str, Any],
        skills: Sequence[Any] | object = UNSET,
        tools: Sequence[Callable[..., Any]] | object = UNSET,
    ) -> None:
        self._signature_declaration = signature
        self._step_name = step_name
        self._runtime_kwargs = dict(runtime_kwargs)
        self._skills_override = skills
        self._tools_override = tools
        self._predictor: Any | None = None
        self._dspy_signature: type[dspy.Signature] | None = None
        self._skills: tuple[Any, ...] = ()
        self._tools: tuple[Callable[..., Any], ...] = ()

    async def __call__(self, **inputs: Any) -> Prediction:
        """Run the configured agent and return its raw DSPy prediction."""
        dspy_signature = self._resolve_signature()
        self._validate_input_names(dspy_signature, inputs)

        if self._predictor is None:
            self._predictor = _build_predictor(
                dspy_signature,
                skills=self._skills,
                tools=self._tools,
                **self._runtime_kwargs,
            )

        from predict_rlm.trace import extract_trace_from_exc

        state = _AgentInvocationState(uuid.uuid4().hex)
        invocation_token = _AGENT_INVOCATION_STATE.set(state)

        try:
            try:
                prediction: Prediction = await self._predictor.acall(**inputs)
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
                            trace, invocation_id=state.invocation_id, evidence=evidence
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
                        trace, invocation_id=state.invocation_id, evidence=evidence
                    )
                input_types = {name: type(value).__name__ for name, value in inputs.items()}
                raise AgentStepExecutionError(
                    f"agent step {self._step_name!r} failed calling "
                    f"{_describe_signature(dspy_signature)}: {exc}. "
                    f"input types: {input_types}."
                ) from exc

            terminal_event = _emit_terminal_trace(
                prediction.trace,
                invocation_id=state.invocation_id,
                evidence=_evidence_metadata(prediction.evidence),
            )

            # Resolve process-local state only in the executing worker. Existing
            # observers have already applied their own strict/error policy.
            from avalanche.evaluation_capture import _STEP_EVALUATION_CAPTURE

            capture = _STEP_EVALUATION_CAPTURE.get()
            if capture is not None:
                capture.submit(inputs, prediction, terminal_event)
            return prediction
        finally:
            _AGENT_INVOCATION_STATE.reset(invocation_token)

    def _resolve_signature(self) -> type[dspy.Signature]:
        if self._dspy_signature is None:
            self._dspy_signature = resolve_signature(
                self._signature_declaration, name=self._step_name
            )
            self._skills = (
                () if self._skills_override is UNSET else tuple(self._skills_override)
            )
            self._tools = () if self._tools_override is UNSET else tuple(self._tools_override)
        return self._dspy_signature

    def _validate_input_names(
        self, dspy_signature: type[dspy.Signature], inputs: Mapping[str, Any]
    ) -> None:
        expected = set(dspy_signature.input_fields)
        received = set(inputs)
        missing = sorted(expected - received)
        unexpected = sorted(received - expected)
        if missing or unexpected:
            problems: list[str] = []
            if missing:
                problems.append(f"missing input fields {missing}")
            if unexpected:
                problems.append(f"unexpected input fields {unexpected}")
            raise AgentStepError(
                f"agent step {self._step_name!r}: " + "; ".join(problems) + "."
            )


class _AgentStepSpec(Generic[InputT, OutputT]):
    """Immutable declaration data plus per-invocation runtime binding."""

    def __init__(
        self,
        step_name: str,
        *,
        signature: type[dspy.Signature],
        runtime_kwargs: Mapping[str, Any],
        skills: Sequence[Any] | object,
        tools: Sequence[Callable[..., Any]] | object,
        public_signature: inspect.Signature,
        evaluations: Evaluations[InputT, OutputT] | None,
    ) -> None:
        self.step_name = step_name
        self.signature = signature
        self.runtime_kwargs = dict(runtime_kwargs)
        self.skills = skills
        self.tools = tools
        self.public_signature = public_signature
        self.evaluations = evaluations

    def make_agent(self) -> Agent:
        defaults = _WORKFLOW_AGENT_DEFAULTS.get()
        return Agent(
            signature=self.signature,
            step_name=self.step_name,
            runtime_kwargs={
                **_AGENT_RUNTIME_DEFAULTS,
                **defaults,
                **self.runtime_kwargs,
            },
            skills=self.skills,
            tools=self.tools,
        )

    def field_schema_metadata(self) -> dict[str, list[dict[str, str]]]:
        """Serialize only the declared invocation field schemas."""
        signature = resolve_signature(self.signature, name=self.step_name)
        return {
            "inputs": _serialize_signature_fields(signature.input_fields, type_key="type"),
            "outputs": _serialize_signature_fields(signature.output_fields, type_key="type"),
        }

    def signature_instruction_line(self) -> str:
        """Return the first non-empty signature instruction line."""
        signature = resolve_signature(self.signature, name=self.step_name)
        instructions = str(getattr(signature, "instructions", "") or "")
        return next(
            (line.strip() for line in instructions.splitlines() if line.strip()),
            "",
        )

    def declaration_metadata(
        self, workflow_defaults: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Serialize static agent declaration state without building a predictor."""
        signature = resolve_signature(self.signature, name=self.step_name)
        skills = () if self.skills is UNSET else tuple(self.skills)
        tools = () if self.tools is UNSET else tuple(self.tools)

        from predict_rlm.rlm_skills import merge_skills

        skill_instructions, packages, modules, skill_tools = merge_skills(list(skills))
        signature_instructions = str(getattr(signature, "instructions", "") or "")
        aggregated_instructions = signature_instructions
        if skill_instructions:
            aggregated_instructions += (
                "\n\n" if aggregated_instructions else ""
            ) + skill_instructions

        return {
            "signature": {
                "name": getattr(signature, "__name__", type(signature).__name__),
                "instructions": signature_instructions,
                "inputs": _serialize_signature_fields(signature.input_fields),
                "outputs": _serialize_signature_fields(signature.output_fields),
            },
            "runtime": _effective_runtime_metadata(
                workflow_defaults or {}, self.runtime_kwargs
            ),
            "models": _effective_model_metadata(workflow_defaults or {}, self.runtime_kwargs),
            "skills": [_serialize_skill(skill) for skill in skills],
            "aggregated_static_instructions": aggregated_instructions,
            "packages": packages,
            "modules": list(modules),
            "tools": [
                *_serialize_tools(skill_tools),
                *_serialize_tools({_callable_name(tool): tool for tool in tools}),
            ],
        }

    def with_workflow_defaults(
        self,
        fn: Callable[..., Any],
        defaults: Mapping[str, Any],
        *,
        classifier_defaults: Mapping[str, JsonValue] | None = None,
    ) -> Callable[..., Any]:
        owned_classifier_defaults = dict(classifier_defaults or {})

        async def bound(*args: Any, **kwargs: Any) -> Any:
            # Resolve process-local state on execution; Ray serializes this closure by value.
            from avalanche.agent.agent_step import _WORKFLOW_AGENT_DEFAULTS
            from avalanche.classifier.classifier_step import _WORKFLOW_CLASSIFIER_DEFAULTS

            token = _WORKFLOW_AGENT_DEFAULTS.set(defaults)
            classifier_token = _WORKFLOW_CLASSIFIER_DEFAULTS.set(owned_classifier_defaults)
            try:
                return await fn(*args, **kwargs)
            finally:
                _WORKFLOW_CLASSIFIER_DEFAULTS.reset(classifier_token)
                _WORKFLOW_AGENT_DEFAULTS.reset(token)

        update_wrapper(bound, fn)
        bound.__signature__ = getattr(fn, "__signature__", inspect.signature(fn))  # type: ignore[attr-defined]
        return bound


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


def _serialize_signature_fields(
    fields: Mapping[str, Any], *, type_key: str = "annotation"
) -> list[dict[str, str]]:
    serialized = []
    for name, field in fields.items():
        extra = getattr(field, "json_schema_extra", None)
        description = getattr(field, "description", None)
        if not isinstance(description, str) and isinstance(extra, Mapping):
            description = extra.get("desc")
        serialized.append(
            {
                "name": name,
                type_key: _annotation_name(getattr(field, "annotation", Any)),
                "description": description if isinstance(description, str) else "",
            }
        )
    return serialized


def _annotation_name(annotation: Any) -> str:
    if annotation is Any:
        return "Any"
    if annotation is None or annotation is types.NoneType:
        return "None"
    if isinstance(annotation, str):
        return annotation
    forward_name = getattr(annotation, "__forward_arg__", None)
    if isinstance(forward_name, str):
        return forward_name

    origin = get_origin(annotation)
    if origin is not None:
        args = get_args(annotation)
        if origin in (Union, types.UnionType):
            return " | ".join(_annotation_name(arg) for arg in args)
        origin_name = getattr(origin, "__qualname__", getattr(origin, "__name__", "type"))
        rendered_args = ", ".join(_annotation_name(arg) for arg in args)
        return f"{origin_name}[{rendered_args}]" if rendered_args else origin_name

    return getattr(
        annotation,
        "__qualname__",
        getattr(annotation, "__name__", type(annotation).__name__),
    )


def _serialize_skill(skill: Any) -> dict[str, Any]:
    tools = getattr(skill, "tools", {})
    modules = getattr(skill, "modules", {})
    return {
        "name": str(getattr(skill, "name", type(skill).__name__)),
        "instructions": str(getattr(skill, "instructions", "") or ""),
        "packages": [
            package for package in getattr(skill, "packages", ()) if isinstance(package, str)
        ],
        "modules": [name for name in modules if isinstance(name, str)],
        "tools": [name for name in tools if isinstance(name, str)],
    }


def _serialize_tools(tools: Mapping[str, Callable[..., Any]]) -> list[dict[str, str]]:
    serialized = []
    for name, tool in tools.items():
        if not isinstance(name, str) or not callable(tool):
            continue
        try:
            source_code = textwrap.dedent(inspect.getsource(tool)).rstrip()
        except (OSError, TypeError):
            source_code = ""
        serialized.append(
            {
                "name": name,
                "description": inspect.getdoc(tool) or "",
                "source_code": source_code,
            }
        )
    return serialized


def _callable_name(value: Callable[..., Any]) -> str:
    return str(getattr(value, "__name__", type(value).__name__))


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
) -> dict[str, dict[str, Any]]:
    """Describe only declaratively supported model sources; never probe DSPy globals."""
    models: dict[str, dict[str, Any]] = {}
    for runtime_key, label in (("lm", "main"), ("sub_lm", "sub")):
        if runtime_key in step_overrides:
            value = step_overrides[runtime_key]
            source = "step override"
        elif runtime_key in workflow_defaults:
            value = workflow_defaults[runtime_key]
            source = "workflow default"
        else:
            models[label] = {"source": "PredictRLM default"}
            continue
        models[label] = {
            "source": source,
            "identity": _strict_model_metadata_value(value, runtime_key),
        }
    return models


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
    if module == "dspy" or module.startswith(("dspy.", "predict_rlm.")):
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


def _reject_nested_agent_decorator() -> None:
    if _workflow_context.get() is not None:
        raise AgentStepError(
            "Agent decorators cannot be declared inside a workflow body; "
            "use ava.agent.step(Signature, inputs=...) for an inline agent."
        )


def _agent_runtime_configuration(
    *,
    owner: str,
    lm: Any,
    sub_lm: Any,
    max_iterations: Any,
    skills: Sequence[Any] | object,
    tools: Sequence[Callable[..., Any]] | object,
    predictor_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    if skills is not UNSET and not isinstance(skills, Sequence):
        raise TypeError(f"{owner} skills must be a sequence")
    if tools is not UNSET:
        if not isinstance(tools, Sequence):
            raise TypeError(f"{owner} tools must be a sequence")
        for tool in tools:
            if not callable(tool):
                raise TypeError(f"{owner} tools must be callable")

    runtime_kwargs = {
        name: value
        for name, value in (
            ("lm", lm),
            ("sub_lm", sub_lm),
            ("max_iterations", max_iterations),
        )
        if value is not UNSET
    }
    runtime_kwargs.update(predictor_kwargs)
    return validate_runtime_kwargs(runtime_kwargs, owner=owner)


def agent_step(
    signature: Any = None,
    *,
    lm: Any = UNSET,
    sub_lm: Any = UNSET,
    max_iterations: Any = UNSET,
    skills: Sequence[Any] | object = UNSET,
    tools: Sequence[Callable[..., Any]] | object = UNSET,
    evaluations: Evaluations[InputT, OutputT] | None = None,
    **predictor_kwargs: Any,
) -> Callable[[Callable[..., Any]], Node]:
    """Register a bodyful workflow step with an injected callable Agent.

    The first positional argument is a subclassed ``ava.Signature``, an inline
    ``ava.agent.Signature(...)``, or another DSPy Signature class.
    """
    _reject_nested_agent_decorator()
    if signature is None:
        raise TypeError("ava.agent_step requires a Signature as its first argument")
    if evaluations is not None and not isinstance(evaluations, Evaluations):
        raise TypeError("ava.agent_step evaluations must be an ava.Evaluations declaration")
    runtime_kwargs = _agent_runtime_configuration(
        owner="ava.agent_step",
        lm=lm,
        sub_lm=sub_lm,
        max_iterations=max_iterations,
        skills=skills,
        tools=tools,
        predictor_kwargs=predictor_kwargs,
    )

    def decorator(user_fn: Callable[..., Any]) -> Node:
        _reject_nested_agent_decorator()
        public_signature = _public_step_signature(user_fn, decoration_namespace())
        step_interface = step_interface_from_signature(public_signature)
        spec = _AgentStepSpec(
            user_fn.__name__,
            signature=signature,
            runtime_kwargs=runtime_kwargs,
            skills=skills,
            tools=tools,
            public_signature=public_signature,
            evaluations=evaluations,
        )

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Resolve process-local capture state only after reaching the worker.
            from avalanche.evaluation_capture import capture_step_evaluations

            with capture_step_evaluations(spec.evaluations, step_name=spec.step_name):
                result = user_fn(*args, **kwargs, agent=spec.make_agent())
                return await result if inspect.isawaitable(result) else result

        update_wrapper(wrapper, user_fn)
        wrapper.__signature__ = spec.public_signature  # type: ignore[attr-defined]
        wrapper.__agent_step__ = spec  # type: ignore[attr-defined]
        return Node(
            wrapper,
            NodeType.STEP,
            num_returns=1,
            step_interface=step_interface,
        )

    return decorator


class _InlineAgentFuture(NodeFuture):
    def __call__(self, user_fn: Callable[..., Any]) -> Never:
        raise AgentStepError(
            "ava.agent.step(...) inside a workflow returns an inline node future, "
            "not a decorator. Declare bodyful agent decorators outside the workflow."
        )


class _InlineAgentNode(Node):
    # One signature field is one value, even when that value is a collection.
    _expand_single_return = False

    def _make_future(
        self,
        *,
        future_id: str,
        node_slug: str,
        graph_ref: DefaultDict[str, list[str]],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> NodeFuture:
        return _InlineAgentFuture(
            node=self,
            future_id=future_id,
            node_slug=node_slug,
            graph_ref=graph_ref,
            args=args,
            kwargs=kwargs,
        )


def step(
    signature: type[dspy.Signature],
    *,
    inputs: Mapping[str, Any] | None = None,
    slug: str | None = None,
    lm: Any = UNSET,
    sub_lm: Any = UNSET,
    max_iterations: Any = UNSET,
    skills: Sequence[Any] | object = UNSET,
    tools: Sequence[Callable[..., Any]] | object = UNSET,
    **predictor_kwargs: Any,
) -> NodeFuture | Callable[[Callable[..., Any]], Node]:
    """Invoke an inline agent in a workflow, or declare a bodyful agent outside it."""
    if _workflow_context.get() is None:
        if inputs is not None or slug is not None:
            raise TypeError("ava.agent.step inputs= and slug= require a workflow body")
        return agent_step(
            signature,
            lm=lm,
            sub_lm=sub_lm,
            max_iterations=max_iterations,
            skills=skills,
            tools=tools,
            **predictor_kwargs,
        )

    signature = resolve_signature(signature, name="inline agent")
    if inputs is not None:
        if not isinstance(inputs, Mapping):
            raise TypeError("ava.agent.step inputs must be a mapping")
        unexpected = [name for name in inputs if name not in signature.input_fields]
        if unexpected:
            raise AgentStepError(f"inline agent has unexpected input fields {unexpected}")
    runtime_kwargs = _agent_runtime_configuration(
        owner="ava.agent.step",
        lm=lm,
        sub_lm=sub_lm,
        max_iterations=max_iterations,
        skills=skills,
        tools=tools,
        predictor_kwargs=predictor_kwargs,
    )
    output_fields = signature.output_fields
    output_adapters = tuple(
        (name, TypeAdapter(field.rebuild_annotation())) for name, field in output_fields.items()
    )
    output_annotations = tuple(field.rebuild_annotation() for field in output_fields.values())
    public_signature = inspect.Signature(
        parameters=[
            inspect.Parameter(
                name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=field.rebuild_annotation(),
            )
            for name, field in signature.input_fields.items()
        ],
        return_annotation=(
            output_annotations[0] if len(output_annotations) == 1 else tuple[output_annotations]
        ),
    )
    spec = _AgentStepSpec(
        signature.__name__,
        signature=signature,
        runtime_kwargs=runtime_kwargs,
        skills=skills,
        tools=tools,
        public_signature=public_signature,
        evaluations=None,
    )

    async def invoke(*args: Any, **kwargs: Any) -> Any:
        from avalanche.evaluation_capture import capture_step_evaluations

        with capture_step_evaluations(None, step_name=spec.step_name):
            bound = public_signature.bind(*args, **kwargs)
            prediction: dspy.Prediction = await spec.make_agent()(**bound.arguments)
            values = []
            for name, adapter in output_adapters:
                try:
                    value = prediction[name]
                except KeyError as exc:
                    raise AgentStepError(
                        f"inline agent {spec.step_name!r} is missing output field {name!r}"
                    ) from exc
                values.append(adapter.validate_python(value, strict=True))
            return values[0] if len(values) == 1 else tuple(values)

    invoke.__name__ = spec.step_name
    invoke.__qualname__ = spec.step_name
    invoke.__doc__ = signature.instructions
    invoke.__signature__ = public_signature  # type: ignore[attr-defined]
    invoke.__agent_step__ = spec  # type: ignore[attr-defined]
    node = _InlineAgentNode(
        invoke,
        NodeType.STEP,
        num_returns=len(output_fields),
        slug=slug,
        step_interface=step_interface_from_signature(public_signature),
    )
    return node(**inputs) if inputs is not None else node()


def _public_step_signature(
    user_fn: Callable[..., Any], localns: dict[str, object] | None
) -> inspect.Signature:
    signature = resolve_step_signature(user_fn, localns)
    agent_parameter = signature.parameters.get("agent")
    if agent_parameter is None:
        raise AgentStepError(
            f"agent step {user_fn.__qualname__!r} requires a keyword-only agent parameter"
        )
    if agent_parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
        raise AgentStepError(
            f"agent step {user_fn.__qualname__!r} agent parameter must be keyword-only"
        )
    if agent_parameter.default is not inspect.Parameter.empty:
        raise AgentStepError(
            f"agent step {user_fn.__qualname__!r} agent parameter is framework-injected "
            "and cannot have a default"
        )

    if agent_parameter.annotation is not Agent:
        raise AgentStepError(
            f"agent step {user_fn.__qualname__!r} agent parameter must be annotated ava.Agent"
        )

    parameters = [
        parameter for name, parameter in signature.parameters.items() if name != "agent"
    ]
    return signature.replace(parameters=parameters)


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


def _describe_signature(dspy_signature: type[dspy.Signature]) -> str:
    inputs = ", ".join(dspy_signature.input_fields)
    outputs = ", ".join(dspy_signature.output_fields)
    return f"signature {dspy_signature.__name__}({inputs}) -> ({outputs})"
