"""Claude Agent SDK traces: the common turns plus session details and cost."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from avalanche.agent.live import AgentLiveEvent
from avalanche.agent.native.claude_agent import ClaudeAgentTraceHeader, ClaudeAgentTurn
from avalanche.agent.trace import AgentTraceHeader

from . import InspectionTurn, PendingStep, TraceSection, TurnView
from .generic import GENERIC, duration_seconds, reasoning_items, usage_label

_LABELS = {
    "session.initialized": "Session started",
    "rate_limit": "Rate limit reported",
}


def _native(header: AgentTraceHeader) -> ClaudeAgentTraceHeader:
    native = header.native
    if not isinstance(native, ClaudeAgentTraceHeader):
        raise TypeError(f"not a Claude Agent SDK trace: {native.format}")
    return native


@dataclass(frozen=True)
class ClaudeAgentViewer:
    def header(self, header: AgentTraceHeader) -> tuple[tuple[str, str], ...]:
        native = _native(header)
        lines = [
            (f"Model: {header.model} · Session: {header.session_id}", ""),
            (
                f"Turns: {header.turn_count} · Duration: {duration_seconds(header.duration_ms)}"
                f" · Result: {native.result_subtype}",
                "",
            ),
            (f"Usage: {usage_label(header.usage)}", ""),
        ]
        if native.permission_denials:
            lines.append((f"Permission denials: {native.permission_denials}", "yellow"))
        lines.extend((f"Error: {error}", "bold red") for error in native.errors)
        return tuple(lines)

    def turn_limit(self, header: AgentTraceHeader | None) -> int | None:
        return None

    def submitted(self, header: AgentTraceHeader) -> bool:
        return header.status == "succeeded"

    def turn(self, turn: InspectionTurn, *, full_output: bool) -> TurnView:
        view = GENERIC.turn(turn, full_output=full_output)
        native = turn.finished.turn.native
        if not isinstance(native, ClaudeAgentTurn):
            raise TypeError(f"not a Claude turn: {native.format}")
        # Rate-limit, billing and authentication failures arrive as API-error turns.
        response = TraceSection(
            "response",
            "Response",
            {"stop_reason": native.stop_reason, "api_error": native.api_error},
            style="bold red" if native.api_error is not None else "dim",
        )
        # Model reasoning stays last, after the response details.
        reasoning = reasoning_items(turn.finished.turn)
        body = view.items[: len(view.items) - len(reasoning)]
        return replace(view, items=(*body, response, *reasoning))

    def live_label(self, event: AgentLiveEvent) -> str:
        if event.native is None:
            return GENERIC.live_label(event)
        return _LABELS[event.native.kind]

    def pending(self, events: Sequence[AgentLiveEvent]) -> tuple[PendingStep, ...]:
        return ()


CLAUDE_AGENT = ClaudeAgentViewer()
