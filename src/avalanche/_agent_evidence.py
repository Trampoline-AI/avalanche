"""Context-local bridge for agent evidence observation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator, Literal, TypeAlias

from .agent.live import AgentLiveEvent, AgentTraceFinished, AgentTraceUnavailable

AgentInvocationId: TypeAlias = str
AGENT_ERROR_CHARACTER_LIMIT = 65_536


# The wire models are the observer contract: listeners receive validated events.
AgentEvidenceEvent: TypeAlias = AgentLiveEvent
AgentTraceFinishedEvent: TypeAlias = AgentTraceFinished
AgentTraceUnavailableEvent: TypeAlias = AgentTraceUnavailable
AgentEvidenceObserverEvent: TypeAlias = (
    AgentLiveEvent | AgentTraceFinished | AgentTraceUnavailable
)
AgentEvidenceListener: TypeAlias = Callable[[AgentEvidenceObserverEvent], None]
ListenerErrorPolicy: TypeAlias = Literal["raise", "ignore"]
_Observer: TypeAlias = tuple[AgentEvidenceListener, ListenerErrorPolicy]

_AGENT_EVIDENCE_OBSERVER: ContextVar[_Observer | None] = ContextVar(
    "avalanche_agent_evidence_observer", default=None
)

_AGENT_LOG_NODE_ID: ContextVar[str | None] = ContextVar(
    "avalanche_agent_log_node_id", default=None
)


@contextmanager
def capture_agent_log_node(node_id: str) -> Iterator[None]:
    """Associate logs emitted by an agent invocation with its workflow node."""
    token = _AGENT_LOG_NODE_ID.set(node_id)
    try:
        yield
    finally:
        _AGENT_LOG_NODE_ID.reset(token)


def current_agent_log_node_id() -> str | None:
    """Return the workflow node currently executing an agent invocation."""
    return _AGENT_LOG_NODE_ID.get()


@contextmanager
def capture_agent_evidence(
    listener: AgentEvidenceListener,
    *,
    errors: ListenerErrorPolicy = "ignore",
) -> Iterator[None]:
    """Observe agent evidence in this context, restoring any outer observer.

    ``errors="ignore"`` preserves standalone agent execution if a listener
    fails. ``errors="raise"`` propagates listener failures to the emitter.
    """
    if errors not in {"raise", "ignore"}:
        raise ValueError("errors must be 'raise' or 'ignore'")
    token = _AGENT_EVIDENCE_OBSERVER.set((listener, errors))
    try:
        yield
    finally:
        _AGENT_EVIDENCE_OBSERVER.reset(token)


def emit_agent_evidence(event: AgentEvidenceObserverEvent) -> None:
    """Notify the current observer according to its explicit error policy."""
    observer = _AGENT_EVIDENCE_OBSERVER.get()
    if observer is None:
        return
    listener, errors = observer
    if errors == "raise":
        listener(event)
        return
    try:
        listener(event)
    except Exception:
        pass


__all__ = [
    "AgentEvidenceEvent",
    "AgentEvidenceListener",
    "AgentEvidenceObserverEvent",
    "AgentInvocationId",
    "AgentTraceFinishedEvent",
    "AgentTraceUnavailableEvent",
    "ListenerErrorPolicy",
    "capture_agent_evidence",
]
