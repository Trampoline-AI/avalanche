"""Utilities for generating DSPy signatures from typed functions."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Annotated, Any, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from avalanche.agent.desc import Desc

if TYPE_CHECKING:
    import dspy


def generate_signature(
    fn,
    *,
    output_field_name: str = "output",
    skip_params: set[str] | None = None,
) -> type[dspy.Signature]:
    """Generate a DSPy signature class from an annotated function.

    Args:
        fn: Function whose parameters become input fields and whose return annotation becomes
            the output field.
        output_field_name: Name to use for the generated output field.
        skip_params: Parameter names to omit before validation. This supports runtime-injected
            agent step parameters; skipped parameters do not need annotations.

    ``Annotated[T, Desc("...")]`` is unwrapped to ``T``. For input fields, the first
    ``Desc`` metadata item becomes the DSPy ``InputField`` description. Return ``Desc``
    metadata is ignored. Missing docstring -> instructions are empty (``""``) instead
    of ``None`` so DSPy does not fabricate default prose.

    Raises:
        TypeError: If a surviving parameter is variadic or unannotated, if the function has
            no return annotation, or if the return annotation is not a pydantic
            ``BaseModel`` subclass or ``list[Model]``.
        ValueError: If ``output_field_name`` collides with a surviving input parameter.
    """
    import dspy

    skipped = skip_params or set()
    parameters = inspect.signature(fn).parameters
    hints = get_type_hints(fn, include_extras=True)

    fields = {}
    for name, parameter in parameters.items():
        if name == "self" or name in skipped:
            continue
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            raise TypeError(
                f"parameter {name!r} of {fn.__qualname__}() is variadic; agent step "
                "parameters must be explicit"
            )
        if name not in hints:
            raise TypeError(
                f"parameter {name!r} of {fn.__qualname__}() has no type annotation; "
                "agent step parameters must be annotated"
            )
        if name == output_field_name:
            raise ValueError(
                f"{fn.__qualname__}() output field {output_field_name!r} collides with "
                "an input parameter"
            )

        annotation, description = _unwrap_annotated(hints[name])
        if description is None:
            input_field = dspy.InputField()
        else:
            input_field = dspy.InputField(desc=description)
        fields[name] = (annotation, input_field)

    if "return" not in hints:
        raise TypeError(f"{fn.__qualname__}() has no return annotation")

    return_annotation, _ = _unwrap_annotated(hints["return"])
    if not _is_supported_return_annotation(return_annotation):
        raise TypeError(
            f"return annotation of {fn.__qualname__}() is {return_annotation!r}; agent step "
            "return annotation must be a pydantic BaseModel subclass or list[Model]"
        )

    fields[output_field_name] = (return_annotation, dspy.OutputField())
    instructions = inspect.getdoc(fn) or ""
    signature = dspy.make_signature(
        fields,
        instructions,
        signature_name=_signature_name(fn.__name__),
    )
    # Pydantic create_model drops an empty __doc__, which makes DSPy fabricate
    # default instructions; re-set this to honor empty instructions.
    signature.instructions = instructions
    return signature


def _unwrap_annotated(annotation: Any) -> tuple[Any, str | None]:
    if get_origin(annotation) is not Annotated:
        return annotation, None

    args = get_args(annotation)
    description = None
    for metadata in args[1:]:
        if isinstance(metadata, Desc):
            description = metadata.description
            break
    return args[0], description


def _is_supported_return_annotation(annotation: Any) -> bool:
    if _is_base_model_subclass(annotation):
        return True

    if get_origin(annotation) is not list:
        return False

    args = get_args(annotation)
    return len(args) == 1 and _is_base_model_subclass(args[0])


def _is_base_model_subclass(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _signature_name(function_name: str) -> str:
    if not function_name.isidentifier():
        return "GeneratedSignature"

    parts = [part for part in function_name.split("_") if part]
    if not parts:
        return "GeneratedSignature"

    candidate = "".join(part[:1].upper() + part[1:] for part in parts) + "Signature"
    return candidate if candidate.isidentifier() else "GeneratedSignature"
