"""Bodyful ``@ava.agent_step`` workflow nodes."""

from __future__ import annotations

import inspect
from contextvars import ContextVar
from functools import update_wrapper
from typing import Any, Callable, Mapping, Sequence

from ..dag import Node, NodeType
from .config import UNSET, validate_runtime_kwargs
from .signature import resolve_signature


class AgentStepError(RuntimeError):
    """An invalid agent-step declaration or agent-call boundary."""


class AgentStepExecutionError(RuntimeError):
    """A PredictRLM invocation failed inside an agent step."""


_WORKFLOW_AGENT_DEFAULTS: ContextVar[Mapping[str, Any]] = ContextVar(
    "avalanche_agent_workflow_defaults", default={}
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

        try:
            return await self._predictor.acall(**inputs)
        except Exception as exc:
            input_types = {name: type(value).__name__ for name, value in inputs.items()}
            raise AgentStepExecutionError(
                f"agent step {self._step_name!r} failed calling "
                f"{_describe_signature(dspy_signature)}: {exc}. "
                f"input types: {input_types}."
            ) from exc

    def _resolve_signature(self) -> Any:
        if self._dspy_signature is None:
            self._dspy_signature = resolve_signature(
                self._signature_declaration, name=self._step_name
            )
            self._skills = (
                () if self._skills_override is UNSET else tuple(self._skills_override)
            )
            self._tools = (
                () if self._tools_override is UNSET else tuple(self._tools_override)
            )
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
        parameter
        for name, parameter in signature.parameters.items()
        if name != "agent"
    ]
    return signature.replace(parameters=parameters)


def _build_predictor(
    signature: Any,
    *,
    skills: tuple[Any, ...],
    tools: tuple[Callable[..., Any], ...],
    **runtime_kwargs: Any,
) -> Any:
    """Build PredictRLM behind a testable, lazy import seam."""
    from predict_rlm import PredictRLM

    return PredictRLM(
        signature,
        skills=list(skills),
        tools=list(tools),
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
