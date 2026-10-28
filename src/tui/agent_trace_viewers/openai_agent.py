"""OpenAI Agents SDK traces: the common turns plus each response's details."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from avalanche.agent.live import AgentLiveEvent
from avalanche.agent.native.openai_agent import OpenAIAgentTraceHeader, OpenAIAgentTurn
from avalanche.agent.trace import AgentTraceHeader

from . import InspectionTurn, PendingStep, TraceSection, TurnView
from .generic import GENERIC, duration_seconds, reasoning_items, usage_label

_LABELS = {"agent.started": "Agent started"}


def _native(header: AgentTraceHeader) -> OpenAIAgentTraceHeader:
    native = header.native
    if not isinstance(native, OpenAIAgentTraceHeader):
        raise TypeError(f"not an OpenAI Agents SDK trace: {native.format}")
    return native


@dataclass(frozen=True)
class OpenAIAgentViewer:
    def header(self, header: AgentTraceHeader) -> tuple[tuple[str, str], ...]:
        native = _native(header)
        lines = [
            (f"Model: {header.model} · Stop: {header.stop_reason}", ""),
            (
                f"Turns: {header.turn_count} of {native.max_turns}"
                f" · Duration: {duration_seconds(header.duration_ms)}"
                f" · Requests: {native.requests}",
                "",
            ),
            (f"Usage: {usage_label(header.usage)}", ""),
        ]
        lines.extend((f"Error: {error}", "bold red") for error in native.errors)
        return tuple(lines)

    def turn_limit(self, header: AgentTraceHeader | None) -> int | None:
        return None if header is None else _native(header).max_turns

    def submitted(self, header: AgentTraceHeader) -> bool:
        return header.status == "succeeded"

    def turn(self, turn: InspectionTurn, *, full_output: bool) -> TurnView:
        view = GENERIC.turn(turn, full_output=full_output)
        native = turn.finished.turn.native
        if not isinstance(native, OpenAIAgentTurn):
            raise TypeError(f"not an OpenAI Agents SDK turn: {native.format}")
        response = TraceSection(
            "response",
            "Response",
            {
                "response_id": native.response_id,
                "model": native.model,
                "reasoning_tokens": native.reasoning_tokens,
                "refusal": native.refusal,
                "answer": native.answer,
            },
            style="bold red" if native.refusal is not None else "dim",
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


OPENAI_AGENT = OpenAIAgentViewer()
