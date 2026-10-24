"""Project PredictRLM run events onto Avalanche's agent evidence channel."""

from __future__ import annotations

import json
import math
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Mapping, TypeAlias

from pydantic import BaseModel

from ..._agent_evidence import (
    AGENT_ERROR_CHARACTER_LIMIT,
    AgentInvocationId,
    emit_agent_evidence,
)
from ...agent.trace import file_input_evidence
from ...runtime.context import File
from ...workspace import Workspace

if TYPE_CHECKING:
    from predict_rlm import IterationStep, RunEvent

    from ...agent.contract import AgentEventSink


class _AgentInvocationState:
    """Task-local evidence state for one agent invocation."""

    def __init__(self, invocation_id: AgentInvocationId, events: AgentEventSink | None) -> None:
        self.invocation_id = invocation_id
        self.events = events
        self.listener_base_exception: BaseException | None = None


_AGENT_INVOCATION_STATE: ContextVar[_AgentInvocationState | None] = ContextVar(
    "avalanche_agent_invocation_state", default=None
)


class _AvalancheEvidenceSink:
    """Project PredictRLM evidence under the bridge's explicit error policy."""

    strict = True

    async def emit(self, event: RunEvent) -> None:
        state = _current_invocation_state()
        projected = _project_evidence_event(
            event,
            invocation_id=state.invocation_id,
        )
        _emit_sink_evidence(projected, state=state)
        if state.events is not None:
            from .mapping import common_event

            common = common_event(event)
            if common is not None:
                try:
                    await state.events.emit(common)
                except BaseException as exc:
                    if not isinstance(exc, Exception) and state.listener_base_exception is None:
                        state.listener_base_exception = exc
                    raise

    async def flush(self, run_id: str) -> None:
        return None

    async def close(self, run_id: str, terminal_event: RunEvent | None = None) -> None:
        if terminal_event is not None:
            state = _current_invocation_state()
            projected = _project_evidence_event(
                terminal_event,
                invocation_id=state.invocation_id,
            )
            _emit_sink_evidence(projected, state=state)


def _current_invocation_state() -> _AgentInvocationState:
    state = _AGENT_INVOCATION_STATE.get()
    if state is None:
        raise RuntimeError("agent evidence emitted outside an agent invocation")
    return state


def _emit_sink_evidence(
    event: dict[str, Any],
    *,
    state: _AgentInvocationState,
) -> None:
    try:
        emit_agent_evidence(event)
    except BaseException as exc:
        if not isinstance(exc, Exception) and state.listener_base_exception is None:
            state.listener_base_exception = exc
        raise


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

    try:
        import predict_rlm
    except ImportError:
        predict_rlm = None
    if predict_rlm is not None and isinstance(value, predict_rlm.File):
        path = value.path
        if isinstance(path, str) and path:
            from .files import bridged_source

            # An ``ava.File`` input is described by its identity, not its temporary path.
            source = bridged_source(path)
            if source is not None:
                return file_input_evidence(source).model_dump(mode="json")
            return {"kind": "predict_rlm_file", "path": path}
        return _unavailable_value("PredictRLM file has no host path")

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
        if key in {"inputs", "outputs", "output"}:
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

    remaining = size(projected) - _MAX_EVIDENCE_VALUE_BYTES
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

    # Even call identities can exceed the fixed budget. Keep a prefix and
    # expose omitted counts both on the event and in SDK-only inspection.
    reasoning = projected["reasoning"]
    assert isinstance(reasoning, str)

    def omission_note(tool_count: int, predict_count: int, group_count: int) -> str:
        return (
            "\n[unavailable: iteration detail exceeds byte limit; omitted "
            f"{tool_count} tool calls, {predict_count} predict calls, "
            f"and {group_count} predict groups]"
        )

    # Reserve the longest possible count note before trimming call collections.
    reserved_reasoning = reasoning + omission_note(
        len(step.tool_calls),
        sum(len(group.calls) for group in step.predict_calls),
        len(step.predict_calls),
    )
    projected["reasoning"] = reserved_reasoning
    remaining += size(reserved_reasoning) - size(reasoning)
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
    projected["reasoning"] = reasoning + omission_note(
        omitted_tools, omitted_predicts, omitted_groups
    )
    if size(projected) > _MAX_EVIDENCE_VALUE_BYTES:
        # Required numeric SDK metadata cannot be replaced by an omission
        # marker without inventing measurements or breaking the SDK schema.
        raise ValueError("iteration inspection metadata exceeds byte limit")
    return projected, {
        "reason": "iteration detail exceeds byte limit",
        "tool_count": omitted_tools,
        "predict_count": omitted_predicts,
        "predict_group_count": omitted_groups,
    }


def _project_evidence_event(
    event: RunEvent,
    *,
    invocation_id: AgentInvocationId,
) -> dict[str, JsonValue]:
    event_kind = event.kind.value
    data = event.data
    projected: dict[str, JsonValue] = {}

    if event_kind == "run.started":
        inputs = data["inputs"]
        projected = {
            "input_fields": sorted(inputs),
            "inputs": _project_agent_value(inputs),
        }
    elif event_kind == "iteration.recorded":
        from predict_rlm import IterationStep

        step = IterationStep.model_validate(data["step"], strict=True)
        bounded_step, omissions = _bounded_iteration_step(step)
        projected = {
            "iteration": step.iteration,
            "duration_ms": step.duration_ms,
            "error": step.error,
            "tool_count": len(step.tool_calls),
            "predict_count": sum(len(group.calls) for group in step.predict_calls),
            "step": bounded_step,
        }
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
        projected = {
            "status": data["status"],
            "outputs": _project_agent_value(data["outputs"]),
        }
    elif event_kind in {"run.failed", "run.cancelled"}:
        projected = {"error_type": data["error_type"], "error": data["error"]}
    if event_kind != "iteration.recorded":
        _bound_live_detail(projected)

    return {
        "kind": "evidence",
        "invocation_id": invocation_id,
        "sequence": event.sequence,
        "event_kind": event_kind,
        "timestamp_ns": event.timestamp_ns,
        "data": projected,
    }
