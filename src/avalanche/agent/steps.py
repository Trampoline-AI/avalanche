"""Bodyful and inline agent workflow nodes."""

from __future__ import annotations

import inspect
import time
import types
import uuid
from contextvars import ContextVar
from functools import update_wrapper
from typing import (
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
from pydantic import JsonValue, TypeAdapter, ValidationError

from .._agent_evidence import emit_agent_evidence
from ..dag import Node, NodeFuture, NodeType, _workflow_context
from ..evaluations import Evaluations
from ..step_interface import (
    decoration_namespace,
    resolve_step_signature,
    step_interface_from_signature,
)
from .contract import (
    AgentOutputs,
    AgentRequest,
    AgentResult,
    AgentRuntime,
    JsonOutputs,
    PythonOutputs,
    SignatureSpec,
    ToolSpec,
)
from .declaration import (
    AgentDeclaration,
    FieldDeclaration,
    SignatureDeclaration,
    tool_declarations,
)
from .defaults import AgentDefaults, effective_runtime, reject_removed_arguments
from .kinds import RuntimeKind
from .live import AgentEvent, AgentLiveEvent, AgentTraceFinished, AgentTraceUnavailable
from .native import NativeEvent
from .signature import resolve_signature
from .trace import AgentTraceHeader, AgentTraceStatus, retained_fields

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class AgentStepError(RuntimeError):
    """An invalid agent-step declaration or agent-call boundary."""


class AgentStepExecutionError(RuntimeError):
    """An agent invocation failed inside an agent step."""


class AgentOutputError(AgentStepExecutionError):
    """An agent's outputs do not match its step's signature."""


class _InvocationEvents:
    """Stamps one invocation's evidence and hands it to the current observer.

    Avalanche, not the runtime, numbers live events, and each invocation ends
    with exactly one final trace event.
    """

    def __init__(self, invocation_id: str, runtime_kind: RuntimeKind) -> None:
        self._invocation_id = invocation_id
        self._runtime_kind = runtime_kind
        self._sequence = 0
        self.terminated = False

    async def emit(self, native: NativeEvent | None, common: AgentEvent | None) -> None:
        self.record(native, common)

    def record(self, native: NativeEvent | None, common: AgentEvent | None) -> None:
        """Stamp and send one live event; ``emit`` without the await."""
        if self.terminated:
            raise AgentStepError(
                f"agent invocation {self._invocation_id} sent a live event "
                "after its final trace"
            )
        self._sequence += 1
        emit_agent_evidence(
            AgentLiveEvent(
                invocation_id=self._invocation_id,
                sequence=self._sequence,
                timestamp_ns=time.time_ns(),
                common=common,
                native=native,
            )
        )

    def finished(
        self, header: AgentTraceHeader, *, outputs: AgentOutputs | None, complete: bool
    ) -> None:
        kept, omitted = (
            (None, ()) if outputs is None else retained_fields(_raw_outputs(outputs))
        )
        self._terminate(
            AgentTraceFinished(
                invocation_id=self._invocation_id,
                header=header,
                outputs=kept,
                omitted_outputs=omitted,
                complete=complete,
            )
        )

    def unavailable(
        self,
        *,
        status: AgentTraceStatus,
        error: str,
        external_run_id: str | None,
        complete: bool,
    ) -> None:
        self._terminate(
            AgentTraceUnavailable(
                invocation_id=self._invocation_id,
                runtime_kind=self._runtime_kind,
                status=status,
                error=error,
                external_run_id=external_run_id,
                complete=complete,
            )
        )

    def _terminate(self, terminal: AgentTraceFinished | AgentTraceUnavailable) -> None:
        if self.terminated:
            raise AgentStepError(
                f"agent invocation {self._invocation_id} sent a second final trace"
            )
        self.terminated = True
        emit_agent_evidence(terminal)


def _raw_outputs(outputs: AgentOutputs) -> Mapping[str, object]:
    match outputs:
        case PythonOutputs(values=values):
            return values
        case JsonOutputs(text=text):
            return _JSON_OBJECT.validate_json(text)


_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


_WORKFLOW_AGENT_DEFAULTS: ContextVar[AgentDefaults] = ContextVar(
    "avalanche_agent_workflow_defaults", default=AgentDefaults()
)


class Agent:
    """Injected callable that executes its step's explicit Signature."""

    def __init__(
        self,
        *,
        signature: type[dspy.Signature],
        step_name: str,
        runtime: AgentRuntime | None = None,
        tools: tuple[Callable[..., Any], ...] = (),
    ) -> None:
        self._signature_declaration = signature
        self._step_name = step_name
        # PredictRLM when no runtime is given; built lazily, so nothing loads here.
        self._runtime = runtime
        self._declared_tools = tools
        self._signature: SignatureSpec | None = None
        self._tools: tuple[ToolSpec, ...] = ()

    async def __call__(self, **inputs: Any) -> AgentResult:
        """Run the agent and return its validated outputs with the invocation's trace."""
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
        if self._runtime is None:
            from ..runtimes.predict_rlm import PredictRLM

            self._runtime = PredictRLM()
        events = _InvocationEvents(request.invocation_id, self._runtime.kind)
        outcome = await self._runtime.invoke(request, events)
        if not events.terminated:
            raise AgentStepError(
                f"agent step {self._step_name!r}: runtime {self._runtime.kind!r} "
                "returned without sending its final trace"
            )
        # The runtime has already sent its terminal trace, so a mismatch is
        # recorded before the step fails, and no evaluation is submitted.
        try:
            outputs = signature.validate_outputs(outcome.outputs)
        except ValidationError as exc:
            raise AgentOutputError(
                f"agent step {self._step_name!r} returned outputs that do not match "
                f"{_describe_signature(signature.dspy_signature)}: {exc}"
            ) from exc
        result = AgentResult(outputs=outputs, trace=outcome.trace)

        # Resolve process-local state only in the executing worker. Existing
        # observers have already applied their own strict/error policy.
        from avalanche.evaluation_capture import _STEP_EVALUATION_CAPTURE

        capture = _STEP_EVALUATION_CAPTURE.get()
        if capture is not None:
            capture.submit(validated_inputs, result.outputs, result.trace)
        return result

    def _resolve_signature(self) -> SignatureSpec:
        if self._signature is None:
            signature = SignatureSpec.from_dspy(
                resolve_signature(self._signature_declaration, name=self._step_name)
            )
            tools = tuple(
                ToolSpec(
                    name=_callable_name(tool),
                    description=inspect.getdoc(tool),
                    function=tool,
                )
                for tool in self._declared_tools
            )
            self._signature, self._tools = signature, tools
        return self._signature

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
        runtime: AgentRuntime | None,
        tools: tuple[Callable[..., Any], ...],
        public_signature: inspect.Signature,
        evaluations: Evaluations[InputT, OutputT] | None,
    ) -> None:
        self.step_name = step_name
        self.signature = signature
        self.runtime = runtime
        self.tools = tools
        self.public_signature = public_signature
        self.evaluations = evaluations

    def make_agent(self) -> Agent:
        return Agent(
            signature=self.signature,
            step_name=self.step_name,
            runtime=effective_runtime(self.runtime, _WORKFLOW_AGENT_DEFAULTS.get()),
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

    def declaration(self, workflow_defaults: AgentDefaults | None = None) -> AgentDeclaration:
        """Describe the step statically, without building a predictor."""
        signature = resolve_signature(self.signature, name=self.step_name)
        default = (workflow_defaults or AgentDefaults()).runtime
        step_runtime = self.runtime
        if step_runtime is None:
            from ..runtimes.predict_rlm import PredictRLM

            # A step without a runtime declares as an empty PredictRLM merged over
            # a PredictRLM default, so models are attributed to the workflow.
            if default is None or default.kind == PredictRLM.kind:
                step_runtime = PredictRLM()
            else:
                step_runtime = default
                default = None
        if default is not None and default.kind == step_runtime.kind:
            runtime_declaration = step_runtime.declaration(workflow_default=default)
        else:
            runtime_declaration = step_runtime.declaration(workflow_default=None)
        return AgentDeclaration(
            signature=SignatureDeclaration(
                name=getattr(signature, "__name__", type(signature).__name__),
                instructions=str(getattr(signature, "instructions", "") or ""),
                inputs=_field_declarations(signature.input_fields),
                outputs=_field_declarations(signature.output_fields),
            ),
            tools=tool_declarations({_callable_name(tool): tool for tool in self.tools}),
            runtime=runtime_declaration,
        )

    def with_workflow_defaults(
        self,
        fn: Callable[..., Any],
        defaults: AgentDefaults | None,
        *,
        classifier_defaults: Mapping[str, JsonValue] | None = None,
    ) -> Callable[..., Any]:
        owned_classifier_defaults = dict(classifier_defaults or {})

        async def bound(*args: Any, **kwargs: Any) -> Any:
            # Resolve process-local state on execution; Ray serializes this closure by value.
            from avalanche.agent.steps import _WORKFLOW_AGENT_DEFAULTS
            from avalanche.classifier.classifier_step import _WORKFLOW_CLASSIFIER_DEFAULTS

            token = _WORKFLOW_AGENT_DEFAULTS.set(defaults or AgentDefaults())
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
    return [
        {
            "name": field.name,
            type_key: field.annotation,
            "description": field.description,
        }
        for field in _field_declarations(fields)
    ]


def _field_declarations(fields: Mapping[str, Any]) -> tuple[FieldDeclaration, ...]:
    declarations = []
    for name, field in fields.items():
        extra = getattr(field, "json_schema_extra", None)
        description = getattr(field, "description", None)
        if not isinstance(description, str) and isinstance(extra, Mapping):
            description = extra.get("desc")
        declarations.append(
            FieldDeclaration(
                name=name,
                annotation=_annotation_name(getattr(field, "annotation", Any)),
                description=description if isinstance(description, str) else "",
            )
        )
    return tuple(declarations)


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


def _callable_name(value: Callable[..., Any]) -> str:
    return str(getattr(value, "__name__", type(value).__name__))


def _reject_nested_agent_decorator() -> None:
    if _workflow_context.get() is not None:
        raise AgentStepError(
            "Agent decorators cannot be declared inside a workflow body; "
            "use ava.agent.step(Signature, inputs=...) for an inline agent."
        )


def _step_configuration(
    *, owner: str, runtime: object, tools: object, removed: Mapping[str, object]
) -> tuple[AgentRuntime | None, tuple[Callable[..., Any], ...]]:
    reject_removed_arguments(owner, removed)
    if runtime is not None and not isinstance(runtime, AgentRuntime):
        raise TypeError(
            f"{owner} runtime must be an agent runtime such as "
            f"ava.runtimes.PredictRLM(...), not {type(runtime).__name__}"
        )
    if not isinstance(tools, Sequence) or isinstance(tools, str):
        raise TypeError(f"{owner} tools must be a sequence")
    for tool in tools:
        if not callable(tool):
            raise TypeError(f"{owner} tools must be callable")
    return runtime, tuple(tools)


def agent_step(
    signature: Any = None,
    *,
    runtime: AgentRuntime | None = None,
    tools: Sequence[Callable[..., Any]] = (),
    evaluations: Evaluations[InputT, OutputT] | None = None,
    **removed: object,
) -> Callable[[Callable[..., Any]], Node]:
    """Register a bodyful workflow step with an injected callable Agent.

    The first positional argument is a subclassed ``ava.Signature``, an inline
    ``ava.agent.Signature(...)``, or another DSPy Signature class. ``runtime``
    defaults to the workflow's ``agent_defaults`` runtime, else PredictRLM.
    """
    _reject_nested_agent_decorator()
    if signature is None:
        raise TypeError("ava.agent_step requires a Signature as its first argument")
    if evaluations is not None and not isinstance(evaluations, Evaluations):
        raise TypeError("ava.agent_step evaluations must be an ava.Evaluations declaration")
    step_runtime, step_tools = _step_configuration(
        owner="ava.agent_step", runtime=runtime, tools=tools, removed=removed
    )

    def decorator(user_fn: Callable[..., Any]) -> Node:
        _reject_nested_agent_decorator()
        public_signature = _public_step_signature(user_fn, decoration_namespace())
        step_interface = step_interface_from_signature(public_signature)
        spec = _AgentStepSpec(
            user_fn.__name__,
            signature=signature,
            runtime=step_runtime,
            tools=step_tools,
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
    runtime: AgentRuntime | None = None,
    tools: Sequence[Callable[..., Any]] = (),
    evaluations: Evaluations[InputT, OutputT] | None = None,
    **removed: object,
) -> NodeFuture | Callable[[Callable[..., Any]], Node]:
    """Invoke an inline agent in a workflow, or declare a bodyful agent outside it."""
    if _workflow_context.get() is None:
        if inputs is not None or slug is not None:
            raise TypeError("ava.agent.step inputs= and slug= require a workflow body")
        return agent_step(
            signature, runtime=runtime, tools=tools, evaluations=evaluations, **removed
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
    step_runtime, step_tools = _step_configuration(
        owner="ava.agent.step", runtime=runtime, tools=tools, removed=removed
    )
    output_fields = signature.output_fields
    output_names = tuple(output_fields)
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
        runtime=step_runtime,
        tools=step_tools,
        public_signature=public_signature,
        evaluations=evaluations,
    )

    async def invoke(*args: Any, **kwargs: Any) -> Any:
        from avalanche.evaluation_capture import capture_step_evaluations

        with capture_step_evaluations(spec.evaluations, step_name=spec.step_name):
            bound = public_signature.bind(*args, **kwargs)
            result = await spec.make_agent()(**bound.arguments)
            values = tuple(getattr(result.outputs, name) for name in output_names)
            return values[0] if len(values) == 1 else values

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
