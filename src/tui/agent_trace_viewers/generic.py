"""The common layer of a trace as every runtime reports it.

Runtime viewers build on these parts; a runtime without details of its own
can use ``GenericViewer`` as is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from avalanche.agent.live import AgentLiveEvent, CodeGenerated, TurnFinished
from avalanche.agent.trace import (
    AgentTrace,
    CodeExecutionAction,
    ModelCallAction,
    TokenUsage,
    ToolCallAction,
    Turn,
)

from . import InspectionTurn, PendingStep, TraceGroup, TraceItem, TraceSection, TurnView

COMMON_LABELS = {
    "turn.finished": "Turn completed",
    "model.started": "Model call started",
    "model.finished": "Model call finished",
    "tool.started": "Tool call started",
    "tool.finished": "Tool call finished",
    "code.generated": "Code generated",
}


def usage_label(usage: TokenUsage | None) -> str:
    if usage is None:
        return "not reported"
    label = f"{usage.input_tokens} in / {usage.output_tokens} out"
    if usage.cost_usd is not None:
        label += f" · ${usage.cost_usd:.6f}"
    return label


def duration_seconds(duration_ms: int) -> str:
    return f"{duration_ms / 1000:.1f}s"


def code_sections(turn: Turn) -> list[TraceSection]:
    """The turn's code and its output; numbered when the turn ran code more than once."""
    actions = [action for action in turn.actions if isinstance(action, CodeExecutionAction)]
    sections: list[TraceSection] = []
    for number, action in enumerate(actions, start=1):
        suffix = "" if len(actions) == 1 else f" {number}"
        key = "" if len(actions) == 1 else str(number)
        sections.append(TraceSection(f"code{key}", f"Code{suffix}", action.code, python=True))
        sections.append(
            TraceSection(
                f"output{key}",
                f"Output{suffix}",
                action.error if action.error is not None else action.output,
                style="red" if action.error is not None else "green",
            )
        )
    return sections


def tool_group(turn: Turn) -> TraceGroup:
    calls = [action for action in turn.actions if isinstance(action, ToolCallAction)]
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


def model_group(turn: Turn) -> TraceGroup:
    calls = [action for action in turn.actions if isinstance(action, ModelCallAction)]
    return TraceGroup(
        "models",
        f"Model calls ({len(calls)})",
        "bold #b38cff",
        tuple(
            TraceGroup(
                str(index),
                f"Model · {call.model}",
                "bold magenta",
                (
                    TraceSection(
                        "detail",
                        "Error" if call.error is not None else "Detail",
                        call.error
                        if call.error is not None
                        else {
                            "duration_ms": call.duration_ms,
                            "usage": (
                                call.usage.model_dump(mode="json")
                                if call.usage is not None
                                else None
                            ),
                        },
                        style="red" if call.error is not None else "dim",
                    ),
                ),
            )
            for index, call in enumerate(calls)
        ),
    )


def turn_failed(turn: Turn) -> bool:
    """A turn fails when code it ran failed; failed tool or model calls may be handled."""
    return any(
        isinstance(action, CodeExecutionAction) and action.error is not None
        for action in turn.actions
    )


def common_items(turn: Turn) -> list[TraceItem]:
    items: list[TraceItem] = []
    if turn.reasoning:
        items.append(TraceSection("reasoning", "Reasoning", turn.reasoning, style="dim italic"))
    if turn.text:
        items.append(TraceSection("text", "Text", turn.text))
    items.extend(code_sections(turn))
    return items


def pending_code(events: Sequence[AgentLiveEvent]) -> list[PendingStep]:
    """Generated code whose turn has not finished yet."""
    finished = {
        (event.invocation_id, event.common.turn.index)
        for event in events
        if isinstance(event.common, TurnFinished)
    }
    return [
        PendingStep(
            event,
            event.common.turn,
            "awaiting execution",
            TraceSection("code", "Code", event.common.code, python=True),
        )
        for event in events
        if isinstance(event.common, CodeGenerated)
        and (event.invocation_id, event.common.turn) not in finished
    ]


@dataclass(frozen=True)
class GenericViewer:
    """Shows the common layer only."""

    def header(self, record: AgentTrace) -> tuple[tuple[str, str], ...]:
        header = record.header
        return (
            (f"Model: {header.model}", ""),
            (
                f"Turns: {header.turn_count}"
                f" · Duration: {duration_seconds(header.duration_ms)}",
                "",
            ),
            (f"Usage: {usage_label(header.usage)}", ""),
        )

    def turn_limit(self, record: AgentTrace | None) -> int | None:
        return None

    def submitted(self, record: AgentTrace) -> bool:
        return record.header.status == "succeeded"

    def turn(self, turn: InspectionTurn, *, full_output: bool) -> TurnView:
        common = turn.finished
        items = common_items(common.turn)
        items.extend((tool_group(common.turn), model_group(common.turn)))
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
        return TurnView(
            index=common.turn.index,
            duration_ms=common.turn.duration_ms,
            counts=f"{common.tool_call_count} tool · {common.model_call_count} model",
            failed=turn_failed(common.turn),
            items=tuple(items),
        )

    def live_label(self, event: AgentLiveEvent) -> str:
        if event.common is not None:
            return COMMON_LABELS[event.common.kind]
        return event.native.kind

    def pending(self, events: Sequence[AgentLiveEvent]) -> tuple[PendingStep, ...]:
        return tuple(pending_code(events))

    def outputs(self, events: Sequence[AgentLiveEvent]) -> Mapping[str, JsonValue] | None:
        return None


GENERIC = GenericViewer()
