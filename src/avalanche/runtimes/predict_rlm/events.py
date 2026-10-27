"""Project PredictRLM run events onto Avalanche's agent evidence channel."""

from __future__ import annotations

import json
import math
from contextvars import ContextVar
from typing import TYPE_CHECKING, Mapping, TypeAlias

from pydantic import BaseModel

from ..._agent_evidence import AGENT_ERROR_CHARACTER_LIMIT
from ...agent.native import predict_rlm as native
from ...agent.trace import file_input_evidence
from ...file import File
from ...workspace import Workspace

if TYPE_CHECKING:
    from predict_rlm import IterationStep, RunEvent
    from predict_rlm.trace import LMUsage
    from predict_rlm.trace import TokenUsage as PredictRLMTokenUsage

    from ...agent.contract import AgentEventSink
    from ...agent.live import AgentEvent
    from ...agent.native.predict_rlm import PredictRLMNativeEvent
    from .files import AgentFileBridge


class _AgentInvocationState:
    """Task-local evidence state for one agent invocation."""

    def __init__(
        self,
        events: AgentEventSink,
        inputs: Mapping[str, object],
        files: AgentFileBridge | None,
    ) -> None:
        self.events = events
        # The step's own inputs: PredictRLM sees mounted host paths for files instead.
        self.inputs = inputs
        # Turns PredictRLM's file outputs into the ``ava.File`` references the step returns.
        self.files = files
        self.listener_base_exception: BaseException | None = None


_AGENT_INVOCATION_STATE: ContextVar[_AgentInvocationState | None] = ContextVar(
    "avalanche_agent_invocation_state", default=None
)


class _AvalancheEvidenceSink:
    """Send PredictRLM's events to the invocation's sink under its error policy."""

    strict = True

    async def emit(self, event: RunEvent) -> None:
        state = _current_invocation_state()
        native, common = project_event(event, inputs=state.inputs, files=state.files)
        try:
            await state.events.emit(native, common)
        except BaseException as exc:
            if not isinstance(exc, Exception) and state.listener_base_exception is None:
                state.listener_base_exception = exc
            raise

    async def flush(self, run_id: str) -> None:
        return None

    async def close(self, run_id: str, terminal_event: RunEvent | None = None) -> None:
        if terminal_event is not None:
            await self.emit(terminal_event)


def _current_invocation_state() -> _AgentInvocationState:
    state = _AGENT_INVOCATION_STATE.get()
    if state is None:
        raise RuntimeError("agent evidence emitted outside an agent invocation")
    return state


JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
_MAX_EVIDENCE_VALUE_BYTES = 4 * 1024 * 1024
_MAX_EVIDENCE_COLLECTION_ITEMS = 10_000
_MAX_EVIDENCE_DEPTH = 32


def _unavailable_value(reason: str) -> dict[str, JsonValue]:
    return {"kind": "unavailable", "reason": reason}


def _project_agent_value(value: object, *, depth: int = 0) -> JsonValue:
    if depth > _MAX_EVIDENCE_DEPTH:
        return _unavailable_value("maximum nesting depth exceeded")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _unavailable_value("non-finite number")

    if isinstance(value, File | Workspace):
        return file_input_evidence(value).model_dump(mode="json")

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        if len(value) > _MAX_EVIDENCE_COLLECTION_ITEMS:
            return _unavailable_value("mapping exceeds item limit")
        projected: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                return _unavailable_value("mapping keys must be strings")
            projected[key] = _project_agent_value(item, depth=depth + 1)
        return projected
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_EVIDENCE_COLLECTION_ITEMS:
            return _unavailable_value("sequence exceeds item limit")
        return [_project_agent_value(item, depth=depth + 1) for item in value]
    return _unavailable_value(f"unsupported value type: {type(value).__name__}")


def _bounded_agent_error(error: str) -> str:
    if len(error) <= AGENT_ERROR_CHARACTER_LIMIT:
        return error
    marker = "\n[unavailable: remaining error text exceeds character limit]"
    return error[: AGENT_ERROR_CHARACTER_LIMIT - len(marker)] + marker


def _bound_live_detail(projected: dict[str, JsonValue]) -> None:
    """Apply inspection policy once, after selecting an event's public fields."""
    if "error" in projected:
        error = projected["error"]
        if not isinstance(error, str):
            raise TypeError("live agent error must be a string")
        projected["error"] = _bounded_agent_error(error)

    def size(value: JsonValue) -> int:
        return len(json.dumps(value, separators=(",", ":")).encode())

    remaining = size(projected) - _MAX_EVIDENCE_VALUE_BYTES
    if remaining <= 0:
        return
    marker = _unavailable_value("live event detail exceeds byte limit")
    text_marker = "[unavailable: live event detail exceeds byte limit]"
    candidates: list[tuple[int, str, JsonValue]] = []
    for key, value in projected.items():
        # Correlation IDs and measured state are not inspection payloads.
        if key in {"call_id", "status", "error_type", "error"}:
            continue
        replacement: JsonValue
        if key in {"inputs", "outputs"}:
            replacement = marker
        elif isinstance(value, str):
            replacement = text_marker
        elif isinstance(value, list):
            replacement = [text_marker]
        else:
            continue
        saving = size(value) - size(replacement)
        if saving > 0:
            candidates.append((saving, key, replacement))
    for saving, key, replacement in sorted(candidates, key=lambda item: item[0], reverse=True):
        projected[key] = replacement
        remaining -= saving
        if remaining <= 0:
            return
    raise ValueError("live event identity metadata exceeds byte limit")


def _bounded_iteration_step(
    step: IterationStep,
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    """Keep an SDK-shaped step within the aggregate inspection-data budget."""

    def fields(model: BaseModel, *, exclude: set[str]) -> dict[str, JsonValue]:
        value = _project_agent_value(model.model_dump(mode="json", exclude=exclude))
        assert isinstance(value, dict)
        return value

    projected = fields(step, exclude={"tool_calls", "predict_calls"})
    tools: list[JsonValue] = []
    groups: list[JsonValue] = []
    projected["tool_calls"] = tools
    projected["predict_calls"] = groups
    for tool in step.tool_calls:
        detail = fields(tool, exclude={"args", "kwargs", "result"})
        args = _project_agent_value(tool.args)
        detail["args"] = args if isinstance(args, list) else [args]
        detail["kwargs"] = _project_agent_value(tool.kwargs)
        detail["result"] = _project_agent_value(tool.result)
        tools.append(detail)
    for group in step.predict_calls:
        detail = fields(group, exclude={"calls"})
        calls: list[JsonValue] = []
        detail["calls"] = calls
        for call in group.calls:
            call_detail = fields(call, exclude={"input", "output"})
            call_detail["input"] = _project_agent_value(call.input)
            call_detail["output"] = _project_agent_value(call.output)
            calls.append(call_detail)
        groups.append(detail)

    def size(value: JsonValue) -> int:
        return len(json.dumps(value, separators=(",", ":")).encode())

    def measured() -> int:
        """The step's size as a turn: each predict call repeats its group's identity."""
        repeated = 0
        for group in groups:
            assert isinstance(group, dict)
            group_calls = group["calls"]
            assert isinstance(group_calls, list)
            if group_calls:
                identity = [group["signature"], group["instructions"], group["model"]]
                repeated += (len(group_calls) - 1) * size(identity)
        return size(projected) + repeated

    remaining = measured() - _MAX_EVIDENCE_VALUE_BYTES
    if remaining <= 0:
        return projected, {}

    # Omit the largest inspection fields first, leaving small detail and SDK
    # identity/timing/usage intact. Replacements retain each field's SDK type.
    marker = _unavailable_value("iteration detail exceeds byte limit")
    text_marker = "[unavailable: iteration detail exceeds byte limit]"
    candidates: list[tuple[int, dict[str, JsonValue], str, JsonValue]] = []

    def collect(value: JsonValue) -> None:
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            # A predict group's identity is repeated on each of its calls in the turn.
            group_calls = (
                value.get("calls") if any(value is group for group in groups) else None
            )
            copies = len(group_calls) if isinstance(group_calls, list) and group_calls else 1
            for key, item in value.items():
                replacement: JsonValue
                if isinstance(item, str):
                    replacement = text_marker
                elif key in {"input", "output", "kwargs", "result"}:
                    replacement = marker
                elif key == "args":
                    replacement = [marker]
                else:
                    collect(item)
                    continue
                saving = size(item) - size(replacement)
                if key in {"signature", "instructions", "model"}:
                    saving *= copies
                if saving > 0:
                    candidates.append((saving, value, key, replacement))

    collect(projected)
    for saving, container, key, replacement in sorted(
        candidates, key=lambda candidate: candidate[0], reverse=True
    ):
        container[key] = replacement
        remaining -= saving
        if remaining <= 0:
            return projected, {}

    # Even call identities can exceed the fixed budget. Keep a prefix of the
    # calls; the event's omissions report how many were left out.
    omitted_tools = 0
    omitted_predicts = 0
    omitted_groups = 0
    while tools and remaining > 0:
        remaining -= size(tools.pop()) + (1 if tools else 0)
        omitted_tools += 1
    for group_detail in reversed(groups):
        assert isinstance(group_detail, dict)
        group_calls = group_detail["calls"]
        assert isinstance(group_calls, list)
        while group_calls and remaining > 0:
            remaining -= size(group_calls.pop()) + (1 if group_calls else 0)
            omitted_predicts += 1
        if remaining <= 0:
            break
    while groups and remaining > 0:
        remaining -= size(groups.pop()) + (1 if groups else 0)
        omitted_groups += 1
    if measured() > _MAX_EVIDENCE_VALUE_BYTES:
        # Required numeric SDK metadata cannot be replaced by an omission
        # marker without inventing measurements or breaking the SDK schema.
        raise ValueError("iteration inspection metadata exceeds byte limit")
    return projected, {
        "reason": "iteration detail exceeds byte limit",
        "tool_count": omitted_tools,
        "predict_count": omitted_predicts,
        "predict_group_count": omitted_groups,
    }


def _file_outputs(
    outputs: Mapping[str, JsonValue], files: AgentFileBridge
) -> dict[str, object]:
    """PredictRLM's outputs with each file as the ``ava.File`` reference the step returns."""
    values: dict[str, object] = {}
    for name, value in outputs.items():
        try:
            values[name] = files.event_output(name, value)
        except (OSError, ValueError) as exc:
            # Never the host path; the step then fails with an output error naming the field.
            values[name] = _unavailable_value(f"not a usable file: {type(exc).__name__}")
    return values


def _project_event_data(
    event: RunEvent,
    *,
    inputs: Mapping[str, object] | None = None,
    files: AgentFileBridge | None = None,
) -> dict[str, JsonValue]:
    """Select one event's public fields, bounded for the live event channel.

    ``inputs`` replaces the run's mounted inputs, so files keep their references, and
    ``files`` describes file outputs as the references the step returns.
    """
    event_kind = event.kind.value
    data = event.data
    projected: dict[str, JsonValue] = {}

    if event_kind == "run.started":
        if inputs is None:
            inputs = data["inputs"]
        projected = {
            "input_fields": sorted(inputs),
            "inputs": _project_agent_value(inputs),
        }
    elif event_kind == "iteration.recorded":
        from predict_rlm import IterationStep

        step = IterationStep.model_validate(data["step"], strict=True)
        bounded_step, omissions = _bounded_iteration_step(step)
        projected = {"step": bounded_step}
        if omissions:
            projected["omissions"] = omissions
    elif event_kind == "predict.started":
        projected = {
            key: data[key] for key in ("call_id", "signature", "instructions", "model")
        }
    elif event_kind == "predict.finished":
        projected = {"call_id": data["call_id"]}
        if "error" in data:
            projected["error"] = data["error"]
    elif event_kind in {"tool.started", "tool.finished"}:
        projected = {"call_id": data["call_id"], "name": data["name"]}
        if "error" in data:
            projected["error"] = data["error"]
    elif event_kind == "code.generated":
        projected = {"iteration": data["iteration"], "code": data["code"]}
    elif event_kind == "code.executed":
        projected = {"iteration": data["iteration"]}
        for key in ("output", "error"):
            if key in data:
                projected[key] = data[key]
    elif event_kind == "run.succeeded":
        outputs = data["outputs"]
        if files is not None:
            outputs = _file_outputs(outputs, files)
        projected = {"status": data["status"], "outputs": _project_agent_value(outputs)}
    elif event_kind in {"run.failed", "run.cancelled"}:
        projected = {"error_type": data["error_type"], "error": data["error"]}
    if event_kind != "iteration.recorded":
        _bound_live_detail(projected)
    return projected


def project_event(
    event: RunEvent,
    *,
    inputs: Mapping[str, object] | None = None,
    files: AgentFileBridge | None = None,
) -> tuple[PredictRLMNativeEvent, AgentEvent | None]:
    """One PredictRLM event as its native payload and its common event, if any.

    ``inputs`` are the step's own inputs, recorded in place of the mounted ones, and
    ``files`` turns file outputs into the step's ``ava.File`` references.
    """
    from ...agent.live import ToolFinished, ToolStarted, TurnFinished
    from .mapping import turn_from_step

    kind = event.kind.value
    data = _project_event_data(event, inputs=inputs, files=files)
    match kind:
        case "run.started":
            return native.RunStarted(
                input_fields=tuple(data["input_fields"]), inputs=data["inputs"]
            ), None
        case "iteration.recorded":
            from predict_rlm import IterationStep

            # The bounded step keeps PredictRLM's schema, with oversized values replaced.
            step = IterationStep.model_validate(data["step"])
            omissions = (
                native.PredictRLMOmissions.model_validate(data["omissions"])
                if "omissions" in data
                else None
            )
            return native.IterationRecorded(omissions=omissions), TurnFinished(
                turn=turn_from_step(step),
                omitted_tool_calls=(
                    omissions.tool_count + omissions.predict_count
                    if omissions is not None
                    else 0
                ),
            )
        case "predict.started":
            return native.PredictStarted(
                call_id=data["call_id"],
                signature=data["signature"],
                instructions=data["instructions"],
                model=data["model"],
            ), None
        case "predict.finished":
            return native.PredictFinished(
                call_id=data["call_id"], predict_error=data.get("error")
            ), None
        case "tool.started":
            return native.Correlated(kind=kind), ToolStarted(
                call_id=data["call_id"], name=data["name"]
            )
        case "tool.finished":
            return native.Correlated(kind=kind), ToolFinished(
                call_id=data["call_id"], name=data["name"], error=data.get("error")
            )
        case "code.generated":
            return native.CodeGenerated(iteration=data["iteration"], code=data["code"]), None
        case "code.executed":
            return native.CodeExecuted(
                iteration=data["iteration"],
                output=data.get("output"),
                execution_error=data.get("error"),
            ), None
        case "run.succeeded":
            return native.RunSucceeded(status=data["status"], outputs=data["outputs"]), None
        case "run.failed" | "run.cancelled":
            return native.RunEnded(
                kind=kind, error_type=data["error_type"], run_error=data["error"]
            ), None
    # Sandbox and artifact lifecycle kinds; an unknown kind fails validation here.
    return native.Lifecycle(kind=kind), None


def native_token_usage(usage: PredictRLMTokenUsage) -> native.PredictRLMTokenUsage:
    return native.PredictRLMTokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost=usage.cost,
        cache_hits=usage.cache_hits,
    )


def native_usage(usage: LMUsage) -> native.PredictRLMUsage:
    return native.PredictRLMUsage(
        main=native_token_usage(usage.main), sub=native_token_usage(usage.sub)
    )
