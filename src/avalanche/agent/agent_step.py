"""Bodyful ``@ava.agent_step`` workflow nodes."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import types
import uuid
from contextvars import ContextVar
from enum import Enum
from functools import update_wrapper
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TypeAlias, Union, get_args, get_origin

from pydantic import BaseModel

from .._agent_evidence import AgentInvocationId, emit_agent_evidence
from ..dag import Node, NodeType
from .config import UNSET, validate_runtime_kwargs
from .signature import resolve_signature


class AgentStepError(RuntimeError):
    """An invalid agent-step declaration or agent-call boundary."""


class AgentStepExecutionError(RuntimeError):
    """An agent invocation failed inside an agent step."""


_WORKFLOW_AGENT_DEFAULTS: ContextVar[Mapping[str, Any]] = ContextVar(
    "avalanche_agent_workflow_defaults", default={}
)


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

    async def emit(self, event: Any) -> None:
        state = _current_invocation_state()
        projected = _project_evidence_event(
            event,
            invocation_id=state.invocation_id,
        )
        _emit_sink_evidence(projected, state=state)

    async def flush(self, run_id: str) -> None:
        return None

    async def close(self, run_id: str, terminal_event: Any | None = None) -> None:
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


def _bounded_agent_value(value: object) -> JsonValue:
    projected = _project_agent_value(value)
    encoded = json.dumps(projected, separators=(",", ":")).encode()
    if len(encoded) > _MAX_EVIDENCE_VALUE_BYTES:
        return _unavailable_value("value exceeds byte limit")
    return projected


def _project_evidence_event(
    event: Any,
    *,
    invocation_id: AgentInvocationId,
) -> dict[str, Any]:
    kind_value = getattr(getattr(event, "kind", None), "value", None)
    event_kind = kind_value if isinstance(kind_value, str) else str(getattr(event, "kind", ""))
    raw_data = getattr(event, "data", {})
    data = dict(raw_data) if isinstance(raw_data, Mapping) else {}
    projected: dict[str, Any] = {}

    if event_kind == "run.started":
        inputs = data.get("inputs")
        projected_inputs = _bounded_agent_value(inputs) if isinstance(inputs, Mapping) else {}
        projected = {
            "input_fields": sorted(inputs) if isinstance(inputs, Mapping) else [],
            "inputs": projected_inputs,
        }
    elif event_kind == "iteration.recorded":
        step = data.get("step")
        step = step if isinstance(step, Mapping) else {}
        projected = {
            "iteration": step.get("iteration"),
            "duration_ms": step.get("duration_ms"),
            "error": step.get("error"),
            "tool_count": len(step.get("tool_calls") or []),
            "predict_count": sum(
                len(group.get("calls") or [])
                for group in (step.get("predict_calls") or [])
                if isinstance(group, Mapping)
            ),
            "step": _bounded_agent_value(step),
        }
    elif event_kind == "predict.started":
        projected = {
            key: data.get(key)
            for key in ("call_id", "signature", "instructions", "model")
            if data.get(key) is not None
        }
    elif event_kind == "predict.finished":
        projected = {
            key: data.get(key) for key in ("call_id", "error") if data.get(key) is not None
        }
    elif event_kind in {"tool.started", "tool.finished"}:
        projected = {
            key: data.get(key)
            for key in ("call_id", "name", "error")
            if data.get(key) is not None
        }
    elif event_kind == "code.generated":
        projected = {
            key: data.get(key) for key in ("iteration", "code") if data.get(key) is not None
        }
    elif event_kind == "code.executed":
        projected = {
            key: data.get(key)
            for key in ("iteration", "output", "error")
            if data.get(key) is not None
        }
    elif event_kind == "run.succeeded":
        projected = {
            "status": data.get("status"),
            "outputs": _bounded_agent_value(data.get("outputs", {})),
        }
    elif event_kind in {"run.failed", "run.cancelled"}:
        projected = {
            key: data.get(key) for key in ("error_type", "error") if data.get(key) is not None
        }

    return {
        "kind": "evidence",
        "invocation_id": invocation_id,
        "sequence": int(getattr(event, "sequence", 0)),
        "event_kind": event_kind,
        "timestamp_ns": int(getattr(event, "timestamp_ns", 0)),
        "data": projected,
    }


def _emit_terminal_trace(
    trace: Any,
    *,
    invocation_id: AgentInvocationId,
) -> bool:
    try:
        exported = trace.to_exportable_json()
        parsed = json.loads(exported)
        if not isinstance(parsed, dict):
            raise TypeError("exported trace is not a JSON object")
    except Exception as exc:
        _emit_trace_unavailable(exc, invocation_id=invocation_id)
        return False
    emit_agent_evidence(
        {
            "kind": "trace_finished",
            "invocation_id": invocation_id,
            "trace": parsed,
        }
    )
    return True


def _emit_trace_unavailable(
    error: Any,
    *,
    invocation_id: AgentInvocationId,
) -> None:
    emit_agent_evidence(
        {
            "kind": "trace_unavailable",
            "invocation_id": invocation_id,
            "error": str(error),
        }
    )


class Agent:
    """Injected callable that executes its step's explicit Signature."""

    def __init__(
        self,
        *,
        signature: Any,
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
        self._dspy_signature: Any | None = None
        self._skills: tuple[Any, ...] = ()
        self._tools: tuple[Callable[..., Any], ...] = ()

    async def __call__(self, **inputs: Any) -> Any:
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

        state = _AgentInvocationState(uuid.uuid4().hex)
        invocation_token = _AGENT_INVOCATION_STATE.set(state)
        try:
            try:
                prediction = await self._predictor.acall(**inputs)
            except asyncio.CancelledError as exc:
                trace = getattr(exc, "trace", None)
                try:
                    if trace is None:
                        _emit_trace_unavailable(exc, invocation_id=state.invocation_id)
                    else:
                        _emit_terminal_trace(trace, invocation_id=state.invocation_id)
                except Exception as evidence_error:
                    try:
                        setattr(exc, "evidence_error", evidence_error)
                    except Exception:
                        pass
                raise
            except Exception as exc:
                if state.listener_base_exception is not None:
                    raise state.listener_base_exception
                trace = getattr(exc, "trace", None)
                if trace is None:
                    _emit_trace_unavailable(exc, invocation_id=state.invocation_id)
                else:
                    _emit_terminal_trace(trace, invocation_id=state.invocation_id)
                input_types = {name: type(value).__name__ for name, value in inputs.items()}
                raise AgentStepExecutionError(
                    f"agent step {self._step_name!r} failed calling "
                    f"{_describe_signature(dspy_signature)}: {exc}. "
                    f"input types: {input_types}."
                ) from exc

            trace = getattr(prediction, "trace", None)
            if trace is None:
                _emit_trace_unavailable(
                    "Agent trace unavailable",
                    invocation_id=state.invocation_id,
                )
            else:
                _emit_terminal_trace(trace, invocation_id=state.invocation_id)
            return prediction
        finally:
            _AGENT_INVOCATION_STATE.reset(invocation_token)

    def _resolve_signature(self) -> Any:
        if self._dspy_signature is None:
            self._dspy_signature = resolve_signature(
                self._signature_declaration, name=self._step_name
            )
            self._skills = (
                () if self._skills_override is UNSET else tuple(self._skills_override)
            )
            self._tools = () if self._tools_override is UNSET else tuple(self._tools_override)
        return self._dspy_signature

    def _validate_input_names(self, dspy_signature: Any, inputs: Mapping[str, Any]) -> None:
        expected_fields = getattr(dspy_signature, "input_fields", None)
        if not isinstance(expected_fields, dict):
            raise AgentStepError(
                f"agent step {self._step_name!r} resolved an invalid DSPy signature"
            )

        expected = set(expected_fields)
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


class _AgentStepSpec:
    """Immutable declaration data plus per-invocation runtime binding."""

    def __init__(
        self,
        user_fn: Callable[..., Any],
        *,
        signature: Any,
        runtime_kwargs: Mapping[str, Any],
        skills: Sequence[Any] | object,
        tools: Sequence[Callable[..., Any]] | object,
    ) -> None:
        self.user_fn = user_fn
        self.step_name = user_fn.__name__
        self.signature = signature
        self.runtime_kwargs = dict(runtime_kwargs)
        self.skills = skills
        self.tools = tools
        self.public_signature = _public_step_signature(user_fn)

    def make_agent(self) -> Agent:
        defaults = _WORKFLOW_AGENT_DEFAULTS.get()
        return Agent(
            signature=self.signature,
            step_name=self.step_name,
            runtime_kwargs={**defaults, **self.runtime_kwargs},
            skills=self.skills,
            tools=self.tools,
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
        self, fn: Callable[..., Any], defaults: Mapping[str, Any]
    ) -> Callable[..., Any]:
        async def bound(*args: Any, **kwargs: Any) -> Any:
            token = _WORKFLOW_AGENT_DEFAULTS.set(defaults)
            try:
                return await fn(*args, **kwargs)
            finally:
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


def _serialize_signature_fields(fields: Mapping[str, Any]) -> list[dict[str, str]]:
    serialized = []
    for name, field in fields.items():
        extra = getattr(field, "json_schema_extra", None)
        description = getattr(field, "description", None)
        if not isinstance(description, str) and isinstance(extra, Mapping):
            description = extra.get("desc")
        serialized.append(
            {
                "name": name,
                "annotation": _annotation_name(getattr(field, "annotation", Any)),
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
    return [
        {
            "name": name,
            "description": inspect.getdoc(tool) or "",
        }
        for name, tool in tools.items()
        if isinstance(name, str) and callable(tool)
    ]


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


def agent_step(
    signature: Any = None,
    *,
    lm: Any = UNSET,
    sub_lm: Any = UNSET,
    max_iterations: Any = UNSET,
    skills: Sequence[Any] | object = UNSET,
    tools: Sequence[Callable[..., Any]] | object = UNSET,
    **predictor_kwargs: Any,
) -> Callable[[Callable[..., Any]], Node]:
    """Register a bodyful workflow step with an injected callable Agent.

    The first positional argument is a subclassed ``ava.Signature``, an inline
    ``ava.agent.Signature(...)``, or another DSPy Signature class. Skills and
    tools are configured only on this decorator.
    """
    if signature is None:
        raise TypeError("ava.agent_step requires a Signature as its first argument")
    if skills is not UNSET and not isinstance(skills, Sequence):
        raise TypeError("ava.agent_step skills must be a sequence")
    if tools is not UNSET:
        if not isinstance(tools, Sequence):
            raise TypeError("ava.agent_step tools must be a sequence")
        for tool in tools:
            if not callable(tool):
                raise TypeError("ava.agent_step tools must be callable")

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
    runtime_kwargs = validate_runtime_kwargs(runtime_kwargs, owner="ava.agent_step")

    def decorator(user_fn: Callable[..., Any]) -> Node:
        spec = _AgentStepSpec(
            user_fn,
            signature=signature,
            runtime_kwargs=runtime_kwargs,
            skills=skills,
            tools=tools,
        )

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = user_fn(*args, **kwargs, agent=spec.make_agent())
            if inspect.isawaitable(result):
                return await result
            return result

        update_wrapper(wrapper, user_fn)
        wrapper.__signature__ = spec.public_signature  # type: ignore[attr-defined]
        wrapper.__agent_step__ = spec  # type: ignore[attr-defined]
        return Node(wrapper, NodeType.STEP, num_returns=1)

    return decorator


# ``ava.agent.step`` is intentionally the same decorator, not another mode.
step = agent_step


def _public_step_signature(user_fn: Callable[..., Any]) -> inspect.Signature:
    signature = inspect.signature(user_fn)
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

    try:
        annotation = inspect.get_annotations(user_fn, eval_str=True).get("agent")
    except Exception:
        annotation = agent_parameter.annotation
    if annotation is not Agent:
        raise AgentStepError(
            f"agent step {user_fn.__qualname__!r} agent parameter must be annotated ava.Agent"
        )

    parameters = [
        parameter for name, parameter in signature.parameters.items() if name != "agent"
    ]
    return signature.replace(parameters=parameters)


def _build_predictor(
    signature: Any,
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


def _describe_signature(dspy_signature: Any) -> str:
    name = getattr(dspy_signature, "__name__", type(dspy_signature).__name__)
    try:
        inputs = ", ".join(dspy_signature.input_fields)
        outputs = ", ".join(dspy_signature.output_fields)
    except Exception:
        return f"signature {name}"
    return f"signature {name}({inputs}) -> ({outputs})"
