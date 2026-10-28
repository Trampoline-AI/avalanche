"""The common layer of a trace as every runtime reports it.

Runtime viewers build on these parts; a runtime without details of its own
can use ``GenericViewer`` as is.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from avalanche.agent.live import AgentLiveEvent
from avalanche.agent.trace import AgentTraceHeader, TokenUsage, ToolCall, Turn

from . import InspectionTurn, PendingStep, TraceGroup, TraceItem, TraceSection, TurnView

COMMON_LABELS = {
    "run.started": "Run started",
    "turn.finished": "Turn completed",
    "tool.started": "Tool call started",
    "tool.finished": "Tool call finished",
}


def usage_label(usage: TokenUsage | None) -> str:
    if usage is None:
        return "not reported"
    # Input counts the prompt cache, which holds most of a cached session's input.
    input_tokens = usage.input_tokens + sum(
        count
        for count in (usage.cache_read_input_tokens, usage.cache_creation_input_tokens)
        if count is not None
    )
    label = f"{input_tokens} in / {usage.output_tokens} out"
    if usage.cost_usd is not None:
        label += f" · ${usage.cost_usd:.6f}"
    return label


def duration_seconds(duration_ms: int) -> str:
    return f"{duration_ms / 1000:.1f}s"


def tool_group(calls: Sequence[ToolCall]) -> TraceGroup:
    return TraceGroup(
        "tools",
        f"Tools ({len(calls)})",
        "bold #b38cff",
        tuple(
            TraceGroup(
                str(index),
                f"Tool · {call.name}",
                "bold magenta",
                (
                    TraceSection("input", "Input", call.arguments),
                    TraceSection(
                        "result",
                        "Error" if call.error is not None else "Result",
                        call.error if call.error is not None else call.result,
                        style="red" if call.error is not None else "dim",
                    ),
                ),
            )
            for index, call in enumerate(calls)
        ),
    )


def message_items(turn: Turn) -> list[TraceItem]:
    if not turn.message:
        return []
    return [TraceSection("message", "Message", turn.message)]


def reasoning_items(turn: Turn) -> list[TraceItem]:
    """The provider's own reasoning, last and collapsed: long, and rarely read."""
    if not turn.reasoning:
        return []
    return [TraceSection("reasoning", "Model reasoning", turn.reasoning, style="dim italic")]


@dataclass(frozen=True)
class GenericViewer:
    """Shows the common layer only."""

    def header(self, header: AgentTraceHeader) -> tuple[tuple[str, str], ...]:
        return (
            (f"Model: {header.model}", ""),
            (
                f"Turns: {header.turn_count}"
                f" · Duration: {duration_seconds(header.duration_ms)}",
                "",
            ),
            (f"Usage: {usage_label(header.usage)}", ""),
        )

    def turn_limit(self, header: AgentTraceHeader | None) -> int | None:
        return None

    def submitted(self, header: AgentTraceHeader) -> bool:
        return header.status == "succeeded"

    def turn(self, turn: InspectionTurn, *, full_output: bool) -> TurnView:
        common = turn.finished
        items = message_items(common.turn)
        items.append(tool_group(common.turn.tool_calls))
        items.append(
            TraceSection(
                "metadata",
                "Metadata",
                {
                    "usage": (
                        common.turn.usage.model_dump(mode="json")
                        if common.turn.usage is not None
                        else None
                    )
                },
            )
        )
        items.extend(reasoning_items(common.turn))
        return TurnView(
            index=common.turn.index,
            duration_ms=common.turn.duration_ms,
            counts=f"{common.tool_call_count} tool",
            failed=common.turn.failed,
            items=tuple(items),
        )

    def live_label(self, event: AgentLiveEvent) -> str:
        if event.common is not None:
            return COMMON_LABELS[event.common.kind]
        return event.kind_label

    def pending(self, events: Sequence[AgentLiveEvent]) -> tuple[PendingStep, ...]:
        return ()


GENERIC = GenericViewer()
