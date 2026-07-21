"""Context-local bridge for best-effort agent evidence observation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator

_AGENT_EVIDENCE_LISTENER: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "avalanche_agent_evidence_listener", default=None
)


@contextmanager
def capture_agent_evidence(
    listener: Callable[[dict[str, Any]], None],
) -> Iterator[None]:
    """Capture evidence emitted by agent calls in the current context."""
    token = _AGENT_EVIDENCE_LISTENER.set(listener)
    try:
        yield
    finally:
        _AGENT_EVIDENCE_LISTENER.reset(token)


def emit_agent_evidence(event: dict[str, Any]) -> None:
    """Notify the current observer without affecting agent execution."""
    listener = _AGENT_EVIDENCE_LISTENER.get()
    if listener is None:
        return
    try:
        listener(event)
    except BaseException:
        pass
