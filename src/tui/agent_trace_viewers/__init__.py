"""How the TUI shows an agent trace, one viewer per trace format.

A viewer turns a trace's common layer and its runtime's native details into
plain data: header lines, a tree of sections for each turn, live-event labels
and the steps still awaiting a turn. The final outputs are common and need no
viewer. The trace widget draws
that data and the store walks the same trees for keyboard navigation, so the
two cannot drift apart. Viewers know nothing about Textual.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from pydantic import JsonValue

from avalanche.agent.live import AgentLiveEvent, TurnFinished
from avalanche.agent.native import NativeEvent
from avalanche.agent.trace import AgentTraceHeader


@dataclass(frozen=True)
class TraceSection:
    """One expandable value: text, code, or a JSON value browsed by key."""

    key: str
    label: str
    value: JsonValue
    style: str = "dim"
    python: bool = False


@dataclass(frozen=True)
class TraceGroup:
    """An expandable heading over further sections and groups."""

    key: str
    label: str
    style: str
    children: tuple[TraceItem, ...]


TraceItem = TraceSection | TraceGroup


@dataclass(frozen=True)
class InspectionTurn:
    """One finished turn: its common event and the runtime's native details, if any."""

    invocation_id: str
    finished: TurnFinished
    native: NativeEvent | None


@dataclass(frozen=True)
class TurnView:
    index: int
    duration_ms: int | None
    counts: str
    failed: bool
    items: tuple[TraceItem, ...]


@dataclass(frozen=True)
class PendingStep:
    """Live work that has no finished turn yet, such as code awaiting execution."""

    event: AgentLiveEvent
    turn: int
    status: str
    section: TraceSection


class TraceFormatViewer(Protocol):
    def header(self, header: AgentTraceHeader) -> tuple[tuple[str, str], ...]:
        """Header lines with their styles, after the status line."""
        ...

    def turn_limit(self, header: AgentTraceHeader | None) -> int | None:
        """The most turns the run may take, when the runtime has a limit."""
        ...

    def submitted(self, header: AgentTraceHeader) -> bool:
        """Whether the last turn returned the run's final answer."""
        ...

    def turn(self, turn: InspectionTurn, *, full_output: bool) -> TurnView: ...

    def live_label(self, event: AgentLiveEvent) -> str: ...

    def pending(self, events: Sequence[AgentLiveEvent]) -> tuple[PendingStep, ...]: ...


def _registry() -> Mapping[str, TraceFormatViewer]:
    from .claude_agent import CLAUDE_AGENT
    from .openai_agent import OPENAI_AGENT
    from .predict_rlm import PREDICT_RLM
    from .pydantic_ai import PYDANTIC_AI

    return MappingProxyType(
        {
            "predict_rlm.run/v1": PREDICT_RLM,
            "claude_agent.messages/v1": CLAUDE_AGENT,
            "openai_agents.responses/v1": OPENAI_AGENT,
            "pydantic_ai.messages/v1": PYDANTIC_AI,
        }
    )


# Keyed by trace format; every format in the native union has a viewer.
VIEWERS: Mapping[str, TraceFormatViewer] = _registry()


def viewer_for(trace_format: str) -> TraceFormatViewer:
    return VIEWERS[trace_format]


def event_trace_format(event: AgentLiveEvent) -> str | None:
    """The trace format a live event shows, when it has a runtime-specific part."""
    if event.native is not None:
        return event.native.format
    if isinstance(event.common, TurnFinished):
        return event.common.turn.native.format
    return None
