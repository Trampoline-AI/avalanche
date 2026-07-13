"""Native DSPy signatures exposed through Avalanche's agent API."""

from __future__ import annotations

from typing import Any

import dspy
from dspy.signatures.signature import SignatureMeta

# These are provider-native field descriptors. Re-exporting them gives class
# declarations the exact semantics users already expect from DSPy.
InputField = dspy.InputField
OutputField = dspy.OutputField


class _AvalancheSignatureMeta(SignatureMeta):
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # Dynamic shorthand. DSPy's own SignatureMeta special-cases only its
        # concrete Signature base, so construct its class explicitly here.
        if self is Signature:
            return dspy.make_signature(*args, **kwargs)
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

    Skills and tools are execution capabilities configured exclusively by
    ``@ava.agent_step(...)``.
    """


def resolve_signature(signature: Any, *, name: str) -> Any:
    """Validate an agent contract."""
    if not isinstance(signature, type) or not issubclass(signature, dspy.Signature):
        raise TypeError(
            "agent signature must be an ava.Signature subclass, an inline "
            "ava.agent.Signature(...), or another DSPy Signature class"
        )
    if not signature.output_fields:
        raise TypeError(f"agent signature {signature.__name__} declares no output fields")
    return signature
