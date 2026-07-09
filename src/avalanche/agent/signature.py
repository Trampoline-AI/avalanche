"""Native DSPy signatures exposed through Avalanche's agent API."""

from __future__ import annotations

from typing import Any, Callable, Sequence

import dspy
from dspy.signatures.signature import SignatureMeta

# These are provider-native field descriptors. Re-exporting them gives class
# declarations the exact semantics users already expect from DSPy.
InputField = dspy.InputField
OutputField = dspy.OutputField


class _AvalancheSignatureMeta(SignatureMeta):
    def __call__(
        cls,
        *args: Any,
        skills: Sequence[Any] = (),
        tools: Sequence[Callable[..., Any]] = (),
        **kwargs: Any,
    ) -> Any:
        # Dynamic shorthand. DSPy's own SignatureMeta special-cases only its
        # concrete Signature base, so construct its class explicitly here.
        if cls is Signature:
            signature = dspy.make_signature(*args, **kwargs)
            _attach_capabilities(signature, skills=skills, tools=tools)
            return signature
        return super().__call__(*args, **kwargs)


class Signature(dspy.Signature, metaclass=_AvalancheSignatureMeta):
    """Base class for typed agent contracts and factory for string contracts.

    Class form::

        class Audit(Signature):
            \"\"\"Audit a supplied document.\"\"\"
            document: str = InputField()
            report: Report = OutputField()

    Dynamic form::

        Signature("document: str -> report: str", "Audit the document.")
    """

    @classmethod
    def from_dspy(
        cls,
        dspy_signature: type[dspy.Signature],
        *,
        skills: Sequence[Any] = (),
        tools: Sequence[Callable[..., Any]] = (),
    ) -> type[dspy.Signature]:
        """Attach capabilities without mutating an existing native signature."""
        if not isinstance(dspy_signature, type) or not issubclass(
            dspy_signature, dspy.Signature
        ):
            raise TypeError("ava.Signature.from_dspy requires a DSPy Signature class")

        class WrappedSignature(dspy_signature):
            pass

        WrappedSignature.__name__ = dspy_signature.__name__
        WrappedSignature.__qualname__ = dspy_signature.__qualname__
        _attach_capabilities(WrappedSignature, skills=skills, tools=tools)
        return WrappedSignature


def resolve_signature(signature: Any, *, name: str) -> tuple[Any, tuple[Any, ...], tuple[Callable[..., Any], ...]]:
    """Validate a native contract and return it with declared capabilities."""
    if not isinstance(signature, type) or not issubclass(signature, dspy.Signature):
        raise TypeError(
            "agent signature must be an ava.Signature subclass, an inline "
            "ava.agent.Signature(...), or ava.Signature.from_dspy(...)"
        )
    if not signature.output_fields:
        raise TypeError(f"agent signature {signature.__name__} declares no output fields")

    skills = tuple(getattr(signature, "__ava_skills__", ()))
    tools = tuple(getattr(signature, "__ava_tools__", ()))
    _validate_tools(tools)
    return signature, skills, tools


def _attach_capabilities(
    signature: type[dspy.Signature],
    *,
    skills: Sequence[Any],
    tools: Sequence[Callable[..., Any]],
) -> None:
    declared_tools = tuple(tools)
    _validate_tools(declared_tools)
    signature.__ava_skills__ = tuple(skills)
    signature.__ava_tools__ = declared_tools


def _validate_tools(tools: tuple[Callable[..., Any], ...]) -> None:
    names: set[str] = set()
    for tool in tools:
        if not callable(tool):
            raise TypeError("agent signature tools must be callable")
        tool_name = getattr(tool, "__name__", None)
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("agent signature tools need a stable non-empty __name__")
        if tool_name in names:
            raise ValueError(f"agent signature tools have duplicate name {tool_name!r}")
        names.add(tool_name)
