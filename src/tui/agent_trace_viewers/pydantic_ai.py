"""Pydantic AI traces: the common turns plus each model response and the run's retries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from avalanche.agent.live import AgentLiveEvent
from avalanche.agent.native.pydantic_ai import PydanticAITraceHeader, PydanticAITurn
from avalanche.agent.trace import AgentTraceHeader

from . import InspectionTurn, PendingStep, TraceSection, TurnView
from .generic import GENERIC, duration_seconds, reasoning_items, usage_label

_LABELS = {
    "run.initialized": "Agent run initialized",
    "tool.retried": "Tool call retried",
}


def _native(header: AgentTraceHeader) -> PydanticAITraceHeader:
    native = header.native
    if not isinstance(native, PydanticAITraceHeader):
        raise TypeError(f"not a Pydantic AI trace: {native.format}")
    return native


@dataclass(frozen=True)
class PydanticAIViewer:
    def header(self, header: AgentTraceHeader) -> tuple[tuple[str, str], ...]:
        native = _native(header)
        lines = [
            (f"Model: {header.model} · Run: {header.external_run_id}", ""),
            (
                f"Turns: {header.turn_count} · Duration: {duration_seconds(header.duration_ms)}"
                f" · Requests: {native.requests} · Stop: {header.stop_reason}",
                "",
            ),
            (f"Usage: {usage_label(header.usage)}", ""),
        ]
        if native.retries:
            lines.append((f"Retries: {native.retries}", "yellow"))
        if native.error is not None:
            lines.append((f"Error: {native.error}", "bold red"))
        return tuple(lines)

    def turn_limit(self, header: AgentTraceHeader | None) -> int | None:
        return None

    def submitted(self, header: AgentTraceHeader) -> bool:
        return header.status == "succeeded"

    def turn(self, turn: InspectionTurn, *, full_output: bool) -> TurnView:
        view = GENERIC.turn(turn, full_output=full_output)
        native = turn.finished.turn.native
        if not isinstance(native, PydanticAITurn):
            raise TypeError(f"not a Pydantic AI turn: {native.format}")
        response = TraceSection(
            "response",
            "Response",
            {
                "model": native.model,
                "provider": native.provider,
                "provider_response_id": native.provider_response_id,
                "finish_reason": native.finish_reason,
            },
            style="bold red" if native.failed else "dim",
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


PYDANTIC_AI = PydanticAIViewer()
