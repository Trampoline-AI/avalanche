"""PredictRLM traces: iteration limits, sub-model calls and per-model usage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from avalanche.agent.live import AgentLiveEvent, CodeGenerated, TurnFinished
from avalanche.agent.native.predict_rlm import (
    CodeExecuted,
    IterationRecorded,
    PredictRLMSubCallGroup,
    PredictRLMTokenUsage,
    RunSucceeded,
)
from avalanche.agent.trace import AgentTrace, CodeExecutionAction, PredictRLMNativeTrace

from . import InspectionTurn, PendingStep, TraceGroup, TraceItem, TraceSection, TurnView
from .generic import duration_seconds, tool_group

_LABELS = {
    "run.started": "Run started",
    "code.generated": "Code generated",
    "code.executed": "Sandbox output received",
    "iteration.recorded": "Turn completed",
    "predict.started": "Sub-model call started",
    "predict.finished": "Sub-model call finished",
    "tool.started": "Tool call started",
    "tool.finished": "Tool call finished",
    "run.succeeded": "Run completed",
    "run.failed": "Run failed",
    "run.cancelled": "Run cancelled",
    "input.prepared": "Input prepared",
    "session.started": "Sandbox started",
    "packages.installed": "Packages installed",
    "artifact.mounted": "File mounted",
    "output.reserved": "Output reserved",
    "artifact.collected": "File collected",
    "output.materialized": "Output written",
    "session.finalized": "Sandbox finalized",
    "session.finalize_failed": "Sandbox finalize failed",
    "session.released": "Sandbox released",
    "session.release_failed": "Sandbox release failed",
}


def _usage_label(usage: PredictRLMTokenUsage) -> str:
    return (
        f"{usage.input_tokens} in / {usage.output_tokens} out"
        f" · {usage.cache_hits} cache · ${usage.cost:.6f}"
    )


def _native(record: AgentTrace) -> PredictRLMNativeTrace:
    native = record.native
    if not isinstance(native, PredictRLMNativeTrace):
        raise TypeError(f"not a PredictRLM trace: {native.format}")
    return native


def _detail(turn: InspectionTurn) -> IterationRecorded:
    if not isinstance(turn.native, IterationRecorded):
        raise TypeError(f"PredictRLM turn has native kind {turn.native.kind!r}")
    return turn.native


def _predict_group(groups: tuple[PredictRLMSubCallGroup, ...]) -> TraceGroup:
    return TraceGroup(
        "predict",
        f"Predict details ({sum(len(group.calls) for group in groups)})",
        "bold #b38cff",
        tuple(
            TraceGroup(
                str(group_index),
                f"Predict · {group.signature} · {group.model}",
                "bold magenta",
                tuple(
                    TraceGroup(
                        str(call_index),
                        f"Call {call_index + 1}",
                        "bold",
                        (
                            TraceSection("input", "Input", call.input),
                            TraceSection(
                                "output",
                                "Error" if call.error is not None else "Output",
                                call.error if call.error is not None else call.output,
                                style="red" if call.error is not None else "dim",
                            ),
                        ),
                    )
                    for call_index, call in enumerate(group.calls)
                ),
            )
            for group_index, group in enumerate(groups)
        ),
    )


@dataclass(frozen=True)
class PredictRLMViewer:
    def header(self, record: AgentTrace) -> tuple[tuple[str, str], ...]:
        header = record.header
        native = _native(record)
        return (
            (f"Model: {header.model} · Sub-model: {native.sub_model}", ""),
            (
                f"Turns: {header.turn_count}/{native.max_iterations}"
                f" · Duration: {duration_seconds(header.duration_ms)}",
                "",
            ),
            (f"Main: {_usage_label(native.usage.main)}", ""),
            (f"Sub:  {_usage_label(native.usage.sub)}", ""),
        )

    def turn_limit(self, record: AgentTrace | None) -> int | None:
        return _native(record).max_iterations if record is not None else None

    def submitted(self, record: AgentTrace) -> bool:
        return record.header.stop_reason == "submit"

    def turn(self, turn: InspectionTurn, *, full_output: bool) -> TurnView:
        common = turn.finished.turn
        detail = _detail(turn)
        [code] = [
            action for action in common.actions if isinstance(action, CodeExecutionAction)
        ]
        items: list[TraceItem] = []
        if common.reasoning:
            items.append(
                TraceSection("reasoning", "Reasoning", common.reasoning, style="dim italic")
            )
        items.append(TraceSection("code", "Code", code.code, python=True))
        items.append(
            TraceSection(
                "output",
                "Output (full)" if full_output else "Output",
                detail.untruncated_output if full_output else code.output,
                style="red" if code.error is not None else "green",
            )
        )
        items.append(tool_group(common))
        items.append(_predict_group(detail.predict_calls))
        items.append(
            TraceSection(
                "metadata",
                "Metadata",
                {
                    "finish_reason": detail.finish_reason,
                    "usage": detail.usage.model_dump(mode="json"),
                },
            )
        )
        return TurnView(
            index=common.index,
            duration_ms=common.duration_ms,
            counts=f"{detail.tool_count} tool · {detail.predict_count} predict",
            failed=code.error is not None,
            items=tuple(items),
        )

    def live_label(self, event: AgentLiveEvent) -> str:
        return _LABELS[event.native.kind]

    def pending(self, events: Sequence[AgentLiveEvent]) -> tuple[PendingStep, ...]:
        finished = {
            (event.invocation_id, event.common.turn.index)
            for event in events
            if isinstance(event.common, TurnFinished)
        }
        steps: list[PendingStep] = []
        for event in events:
            native = event.native
            match event.common, native:
                case CodeGenerated(turn=turn, code=code), _:
                    status, section = (
                        "awaiting execution",
                        TraceSection("code", "Code", code, python=True),
                    )
                case None, CodeExecuted(iteration=turn, output=output):
                    if native.error is not None:
                        status = "execution failed"
                        section = TraceSection("error", "Error", native.error)
                    else:
                        status = "awaiting iteration record"
                        section = TraceSection("output", "Output", output)
                case _:
                    continue
            if (event.invocation_id, turn) not in finished:
                steps.append(PendingStep(event, turn, status, section))
        return tuple(steps)

    def outputs(self, events: Sequence[AgentLiveEvent]) -> Mapping[str, JsonValue] | None:
        for event in reversed(events):
            if isinstance(event.native, RunSucceeded):
                outputs = event.native.outputs
                if not isinstance(outputs, dict):
                    raise TypeError("PredictRLM run outputs must be an object")
                return outputs
        return None


PREDICT_RLM = PredictRLMViewer()
