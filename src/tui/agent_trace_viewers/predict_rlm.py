"""PredictRLM traces: iteration limits, sub-model calls and per-model usage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from avalanche.agent.live import AgentLiveEvent, TurnFinished
from avalanche.agent.native.predict_rlm import (
    CodeExecuted,
    CodeGenerated,
    IterationRecorded,
    PredictRLMPredictCall,
    PredictRLMTokenUsage,
    PredictRLMTraceHeader,
    PredictRLMTurn,
    RunSucceeded,
)
from avalanche.agent.trace import AgentTrace, ToolCall, Turn

from . import InspectionTurn, PendingStep, TraceGroup, TraceItem, TraceSection, TurnView
from .generic import duration_seconds, message_items, reasoning_items, tool_group

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


def _native(record: AgentTrace) -> PredictRLMTraceHeader:
    native = record.header.native
    if not isinstance(native, PredictRLMTraceHeader):
        raise TypeError(f"not a PredictRLM trace: {native.format}")
    return native


def _detail(turn: InspectionTurn) -> IterationRecorded:
    if not isinstance(turn.native, IterationRecorded):
        raise TypeError(f"PredictRLM turn has native kind {turn.native.kind!r}")
    return turn.native


def _turn_native(turn: Turn) -> PredictRLMTurn:
    if not isinstance(turn.native, PredictRLMTurn):
        raise TypeError(f"not a PredictRLM turn: {turn.native.format}")
    return turn.native


def predict_tool_calls(turn: Turn) -> tuple[ToolCall, ...]:
    """The turn's ``predict`` tool calls, paired in order with its native predict calls."""
    count = len(_turn_native(turn).predict_calls)
    return turn.tool_calls[len(turn.tool_calls) - count :]


def _predict_group(
    calls: tuple[ToolCall, ...], details: tuple[PredictRLMPredictCall, ...]
) -> TraceGroup:
    return TraceGroup(
        "predict",
        f"Predict calls ({len(calls)})",
        "bold #b38cff",
        tuple(
            TraceGroup(
                str(index),
                f"Predict · {detail.signature} · {detail.model}",
                "bold magenta",
                (
                    TraceSection("input", "Input", call.arguments),
                    TraceSection(
                        "output",
                        "Error" if call.error is not None else "Output",
                        call.error if call.error is not None else call.result,
                        style="red" if call.error is not None else "dim",
                    ),
                    TraceSection(
                        "detail",
                        "Detail",
                        {
                            "instructions": detail.instructions,
                            "duration_ms": call.duration_ms,
                            "finish_reason": detail.finish_reason,
                            "usage": detail.usage.model_dump(mode="json"),
                        },
                        style="dim",
                    ),
                ),
            )
            for index, (call, detail) in enumerate(zip(calls, details, strict=True))
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
        native = _turn_native(common)
        omissions = _detail(turn).omissions
        predicts = predict_tool_calls(common)
        tools = common.tool_calls[: len(common.tool_calls) - len(predicts)]
        items: list[TraceItem] = message_items(common)
        items.append(TraceSection("code", "Code", native.code, python=True))
        items.append(
            TraceSection(
                "output",
                "Output (full)" if full_output else "Output",
                native.untruncated_output if full_output else native.output,
                style="red" if native.error else "green",
            )
        )
        items.append(tool_group(tools))
        items.append(_predict_group(predicts, native.predict_calls))
        items.append(
            TraceSection(
                "metadata",
                "Metadata",
                {
                    "finish_reason": native.finish_reason,
                    "usage": native.usage.model_dump(mode="json"),
                },
            )
        )
        items.extend(reasoning_items(common))
        tool_count = len(tools) + (omissions.tool_count if omissions is not None else 0)
        predict_count = len(predicts) + (
            omissions.predict_count if omissions is not None else 0
        )
        return TurnView(
            index=common.index,
            duration_ms=common.duration_ms,
            counts=f"{tool_count} tool · {predict_count} predict",
            failed=common.failed,
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
                case None, CodeGenerated(iteration=turn, code=code):
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
