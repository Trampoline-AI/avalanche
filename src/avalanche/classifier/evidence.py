"""Task-local classifier lifecycle observation, independent of step return values."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .models import ClassifierInvocation

ClassifierEvidenceListener = Callable[[ClassifierInvocation], None]
_CLASSIFIER_LISTENER: ContextVar[ClassifierEvidenceListener | None] = ContextVar(
    "avalanche_classifier_evidence_listener", default=None
)


@contextmanager
def capture_classifier_evidence(listener: ClassifierEvidenceListener) -> Iterator[None]:
    """Capture lifecycle snapshots in this context and inherited async/thread contexts."""
    token = _CLASSIFIER_LISTENER.set(listener)
    try:
        yield
    finally:
        _CLASSIFIER_LISTENER.reset(token)


def emit_classifier_evidence(record: ClassifierInvocation) -> None:
    listener = _CLASSIFIER_LISTENER.get()
    if listener is not None:
        # The user may mutate nested result dictionaries after the call returns.
        # An observer owns its snapshot and never shares those mutable containers.
        listener(record.model_copy(deep=True))
