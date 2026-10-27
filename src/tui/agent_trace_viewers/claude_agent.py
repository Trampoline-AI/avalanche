"""Claude Agent SDK traces: the common turns plus session details and cost."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from pydantic import JsonValue

from avalanche.agent.live import AgentLiveEvent
from avalanche.agent.native.claude_agent import (
    ClaudeAgentTraceHeader,
    ClaudeAgentTurn,
    Finished,
)
from avalanche.agent.trace import AgentTrace

from . import InspectionTurn, PendingStep, TraceSection, TurnView
from .generic import COMMON_LABELS, GENERIC, duration_seconds, reasoning_items, usage_label

_LABELS = {
    "started": "Prompt sent",
    "session.initialized": "Session started",
    "tool.used": COMMON_LABELS["tool.started"],
    "tool.returned": COMMON_LABELS["tool.finished"],
    "message.finished": COMMON_LABELS["turn.finished"],
    "rate_limit": "Rate limit reported",
    "finished": "Session finished",
}


def _native(record: AgentTrace) -> ClaudeAgentTraceHeader:
    native = record.header.native
    if not isinstance(native, ClaudeAgentTraceHeader):
        raise TypeError(f"not a Claude Agent SDK trace: {native.format}")
    return native


@dataclass(frozen=True)
class ClaudeAgentViewer:
    def header(self, record: AgentTrace) -> tuple[tuple[str, str], ...]:
        header = record.header
        native = _native(record)
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

    def turn_limit(self, record: AgentTrace | None) -> int | None:
        return None

    def submitted(self, record: AgentTrace) -> bool:
        return record.header.status == "succeeded"

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
        return _LABELS[event.native.kind]

    def pending(self, events: Sequence[AgentLiveEvent]) -> tuple[PendingStep, ...]:
        return ()

    def outputs(self, events: Sequence[AgentLiveEvent]) -> Mapping[str, JsonValue] | None:
        for event in reversed(events):
            if isinstance(event.native, Finished):
                output = event.native.structured_output
                # A failed session reports no output; an oversized one is left out.
                if not isinstance(output, dict):
                    return None
                # Workspace fields hold the session's working directory, not Claude's JSON.
                return {
                    **output,
                    **dict.fromkeys(
                        event.native.workspace_outputs,
                        "workspace: the session's working directory",
                    ),
                }
        return None


CLAUDE_AGENT = ClaudeAgentViewer()
