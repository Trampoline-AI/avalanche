"""Context-local bridge for agent evidence observation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, Literal, TypeAlias, TypedDict

AgentInvocationId: TypeAlias = str


class AgentEvidenceEvent(TypedDict):
    """Incremental, sanitized evidence emitted during an agent run."""

    kind: Literal["evidence"]
    invocation_id: AgentInvocationId
    sequence: int
    event_kind: str
    timestamp_ns: int
    data: dict[str, Any]


class AgentTraceFinishedEvent(TypedDict):
    """Terminal exported trace for a completed, failed, or cancelled agent call."""

    kind: Literal["trace_finished"]
    invocation_id: AgentInvocationId
    trace: dict[str, Any]


class AgentTraceUnavailableEvent(TypedDict):
    """Terminal notification emitted when no exportable trace is available."""

    kind: Literal["trace_unavailable"]
    invocation_id: AgentInvocationId
    error: str


AgentEvidenceObserverEvent: TypeAlias = (
    AgentEvidenceEvent | AgentTraceFinishedEvent | AgentTraceUnavailableEvent
)
AgentEvidenceListener: TypeAlias = Callable[[AgentEvidenceObserverEvent], None]
ListenerErrorPolicy: TypeAlias = Literal["raise", "ignore"]
_Observer: TypeAlias = tuple[AgentEvidenceListener, ListenerErrorPolicy]

_AGENT_EVIDENCE_OBSERVER: ContextVar[_Observer | None] = ContextVar(
    "avalanche_agent_evidence_observer", default=None
)


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
