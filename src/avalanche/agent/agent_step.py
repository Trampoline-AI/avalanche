"""Bodyful and inline agent workflow nodes."""

from __future__ import annotations

import inspect
import textwrap
import types
import uuid
from contextvars import ContextVar
from functools import update_wrapper
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    DefaultDict,
    Generic,
    Mapping,
    Never,
    Sequence,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

import dspy
from dspy import Prediction
from pydantic import JsonValue, TypeAdapter, ValidationError

from ..dag import Node, NodeFuture, NodeType, _workflow_context
from ..evaluations import Evaluations
from ..step_interface import (
    decoration_namespace,
    resolve_step_signature,
    step_interface_from_signature,
)
from .config import UNSET, validate_runtime_kwargs
from .contract import AgentRequest, SignatureSpec, ToolSpec
from .signature import resolve_signature

if TYPE_CHECKING:
    from ..runtimes.predict_rlm import PredictRLM

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
        self._signature: SignatureSpec | None = None
        self._tools: tuple[ToolSpec, ...] = ()
        self._runtime: PredictRLM | None = None

    async def __call__(self, **inputs: Any) -> Prediction:
        """Run the configured agent and return its raw DSPy prediction."""
        signature = self._resolve_signature()
        self._validate_input_names(signature.dspy_signature, inputs)
        try:
            validated_inputs = signature.input_model.model_validate(inputs)
        except ValidationError as exc:
            raise AgentStepError(f"agent step {self._step_name!r}: {exc}") from exc
        request = AgentRequest(
            invocation_id=uuid.uuid4().hex,
            step_name=self._step_name,
            signature=signature,
            inputs=validated_inputs,
            tools=self._tools,
        )
        call = await self._resolve_runtime().predict(request)

        # Resolve process-local state only in the executing worker. Existing
        # observers have already applied their own strict/error policy.
        from avalanche.evaluation_capture import _STEP_EVALUATION_CAPTURE

        capture = _STEP_EVALUATION_CAPTURE.get()
        if capture is not None:
            capture.submit(inputs, call.prediction, call.terminal_event)
        return call.prediction

    def _resolve_signature(self) -> SignatureSpec:
        if self._signature is None:
            self._signature = SignatureSpec.from_dspy(
                resolve_signature(self._signature_declaration, name=self._step_name)
            )
            tools = () if self._tools_override is UNSET else tuple(self._tools_override)
            self._tools = tuple(
                ToolSpec(
                    name=_callable_name(tool),
                    description=inspect.getdoc(tool),
                    function=tool,
                )
                for tool in tools
            )
        return self._signature

    def _resolve_runtime(self) -> PredictRLM:
        if self._runtime is None:
            from ..runtimes.predict_rlm import PredictRLM

            skills = () if self._skills_override is UNSET else tuple(self._skills_override)
            self._runtime = PredictRLM.from_runtime_kwargs(self._runtime_kwargs, skills=skills)
        return self._runtime

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

        from ..runtimes.predict_rlm.runtime import (
            _effective_model_metadata,
            _effective_runtime_metadata,
            _serialize_skill,
        )

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
    evaluations: Evaluations[InputT, OutputT] | None = None,
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
            evaluations=evaluations,
            **predictor_kwargs,
        )

    if evaluations is not None and not isinstance(evaluations, Evaluations):
        raise TypeError("ava.agent.step evaluations must be an ava.Evaluations declaration")
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
        evaluations=evaluations,
    )

    async def invoke(*args: Any, **kwargs: Any) -> Any:
        from avalanche.evaluation_capture import capture_step_evaluations

        with capture_step_evaluations(spec.evaluations, step_name=spec.step_name):
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


def _describe_signature(dspy_signature: type[dspy.Signature]) -> str:
    inputs = ", ".join(dspy_signature.input_fields)
    outputs = ", ".join(dspy_signature.output_fields)
    return f"signature {dspy_signature.__name__}({inputs}) -> ({outputs})"
