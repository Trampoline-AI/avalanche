"""Run one agent call through the Claude Agent SDK; the only module importing the SDK."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import shutil
import tempfile
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin, get_type_hints

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLINotFoundError,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    TypeAdapter,
    ValidationError,
    create_model,
)
from pydantic_core import to_json, to_jsonable_python

from ...agent.contract import (
    AgentEventSink,
    AgentRequest,
    AgentRunOutcome,
    JsonOutputs,
    PythonOutputs,
    ToolSpec,
)
from ...agent.live import ToolFinished, ToolStarted, TurnFinished
from ...agent.native import claude_agent as native
from ...agent.steps import AgentOutputError, AgentStepError, AgentStepExecutionError
from ...agent.trace import (
    AgentTrace,
    AgentTraceHeader,
    AgentTraceStatus,
    ClaudeAgentNativeTrace,
    TokenUsage,
    ToolCallAction,
    Turn,
)
from ...runtime.context import File
from ...workspace import Workspace

if TYPE_CHECKING:
    from .runtime import ClaudeAgent

_SERVER = "avalanche"
_JSON = TypeAdapter(JsonValue)
# Tool arguments and results kept in a trace; larger values are replaced by a marker.
_MAX_VALUE_BYTES = 256 * 1024
# A turn's tool calls kept in its live event; later calls are counted as omitted, so the
# event stays well under the operator's 8 MiB agent-event limit.
_MAX_TURN_ACTION_BYTES = 4 * 1024 * 1024
# An error reason taken from a result's free text is cut to this length.
_MAX_ERROR_CHARS = 4096


async def run(
    runtime: ClaudeAgent, request: AgentRequest, events: AgentEventSink
) -> AgentRunOutcome:
    signature = request.signature
    _check_inputs(request)
    workspace_inputs = [
        name
        for name, info in signature.input_model.model_fields.items()
        if info.annotation is Workspace
    ]
    if len(workspace_inputs) > 1:
        raise AgentStepError(
            f"agent step {request.step_name!r}: ClaudeAgent accepts at most one ava.Workspace "
            f"input, got {workspace_inputs}"
        )
    workspace_outputs = [
        name
        for name, info in signature.output_model.model_fields.items()
        if info.annotation is Workspace
    ]
    unsupported_outputs = [
        name
        for name, info in signature.output_model.model_fields.items()
        if _mentions(info.annotation, File)
        or (info.annotation is not Workspace and _mentions(info.annotation, Workspace))
    ]
    if unsupported_outputs:
        raise AgentStepError(
            f"agent step {request.step_name!r}: ClaudeAgent cannot return ava.File outputs "
            f"or a workspace other than a plain ava.Workspace field {unsupported_outputs}; "
            "return an ava.Workspace instead"
        )
    workspace: Workspace | None = None
    with contextlib.ExitStack() as stack:
        if workspace_inputs:
            workspace = getattr(request.inputs, workspace_inputs[0])
            # A reference workspace runs in place, so the agent's writes land in the
            # host's directory; a value workspace runs in its temporary copy.
            directory = workspace.path
        else:
            directory = Path(
                stack.enter_context(tempfile.TemporaryDirectory(prefix="avalanche-claude-"))
            )
        # Input files go to a fresh folder that is removed afterwards, so a reused or
        # host-owned working directory keeps no copies and runs never collide.
        inputs_directory = Path(tempfile.mkdtemp(prefix=".avalanche-inputs-", dir=directory))
        # Also on any failure before the session starts; the explicit removal below
        # runs first, before a workspace snapshot.
        stack.callback(shutil.rmtree, inputs_directory, ignore_errors=True)
        prompt_inputs = _prompt_inputs(request, directory, inputs_directory)
        options = ClaudeAgentOptions(
            model=runtime.model,
            max_turns=runtime.max_turns,
            max_budget_usd=runtime.max_budget_usd,
            permission_mode=runtime.permission_mode,
            # User and project Claude settings never leak into a workflow step.
            setting_sources=[],
            tools=list(runtime.builtin_tools),
            allowed_tools=[
                *runtime.builtin_tools,
                *(f"mcp__{_SERVER}__{spec.name}" for spec in request.tools),
            ],
            mcp_servers={_SERVER: _tool_server(request.tools)} if request.tools else {},
            # Only the step's own tools: no MCP servers or connectors from the user's
            # Claude account or configuration.
            strict_mcp_config=True,
            system_prompt=_system_prompt(signature.instructions, runtime.system_prompt_append),
            output_format={
                "type": "json_schema",
                "schema": _output_schema(signature.output_model, workspace_outputs),
            },
            cwd=str(directory),
        )
        transcript = _Transcript(events)
        try:
            # Claude gets the full inputs; the trace keeps each one within the value limit.
            await events.emit(
                native.Started(
                    model=runtime.model,
                    inputs={name: _bounded(value) for name, value in prompt_inputs.items()},
                ),
                None,
            )
            try:
                # Closing the stream stops the CLI even when handling a message raises.
                async with contextlib.aclosing(
                    query(prompt=_prompt(prompt_inputs), options=options)
                ) as messages:
                    async for message in messages:
                        await transcript.add(message)
                        if transcript.result is not None:
                            # The CLI exits non-zero after an error result and the SDK
                            # then raises ResultError; the result already says why.
                            break
            except asyncio.CancelledError:
                events.unavailable(
                    status="cancelled",
                    error="the agent call was cancelled",
                    external_run_id=transcript.session_id,
                    complete=True,
                )
                raise
            except CLINotFoundError as exc:
                events.unavailable(
                    status="failed", error=str(exc), external_run_id=None, complete=True
                )
                raise AgentStepExecutionError(
                    f"agent step {request.step_name!r}: the Claude CLI is not installed where "
                    "this step runs; install Claude Code on every machine and Ray worker that "
                    "runs ClaudeAgent steps"
                ) from exc
            except Exception as exc:
                events.unavailable(
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    external_run_id=transcript.session_id,
                    complete=True,
                )
                raise AgentStepExecutionError(
                    f"agent step {request.step_name!r} failed calling Claude: {exc}"
                ) from exc
        finally:
            # Before any workspace snapshot, so the outputs never include the inputs.
            shutil.rmtree(inputs_directory, ignore_errors=True)
        result = transcript.result
        if result is None:
            events.unavailable(
                status="failed",
                error="the Claude CLI ended without a result",
                external_run_id=transcript.session_id,
                complete=True,
            )
            raise AgentStepExecutionError(
                f"agent step {request.step_name!r}: the Claude CLI ended without a result"
            )
        record = transcript.record(result, model=runtime.model)
        events.finished(
            record.model_copy(update={"turns": (), "turns_streamed": True}), complete=True
        )
        if record.header.status != "succeeded":
            detail = "; ".join(_result_errors(result)) or result.subtype
            if result.subtype == "success" and not result.is_error:
                detail = (
                    "the session succeeded without the structured output the signature needs"
                    if result.structured_output is None
                    else "Claude's structured output is not an object"
                )
            raise AgentStepExecutionError(
                f"agent step {request.step_name!r}: Claude did not finish: {detail}"
            )
        if not workspace_outputs:
            return AgentRunOutcome(
                outputs=JsonOutputs(text=json.dumps(result.structured_output)), trace=record
            )
        # A reference snapshot is bound to its directory, which a JSON round trip loses,
        # so Claude's values are validated here and the snapshot joins them as an object.
        snapshot = (
            workspace.snapshot() if workspace is not None else Workspace.from_path(directory)
        )
        outputs: dict[str, object] = dict(result.structured_output)
        for name in workspace_outputs:
            outputs[name] = json.loads(snapshot.model_dump_json())
        try:
            validated = signature.validate_outputs(JsonOutputs(text=json.dumps(outputs)))
        except ValidationError as exc:
            raise AgentOutputError(
                f"agent step {request.step_name!r}: Claude's structured output does not match "
                f"the signature: {exc}"
            ) from exc
        values = {
            name: getattr(validated, name) for name in signature.output_model.model_fields
        }
        values.update(dict.fromkeys(workspace_outputs, snapshot))
        return AgentRunOutcome(outputs=PythonOutputs(values=values), trace=record)


def _prompt_inputs(
    request: AgentRequest, directory: Path, inputs_directory: Path
) -> dict[str, JsonValue]:
    """The inputs as JSON; files are written under the working directory and named by path."""
    values: dict[str, JsonValue] = {}
    written = 0

    def write(file: File) -> str:
        nonlocal written
        written += 1
        target = inputs_directory / str(written) / _file_name(file.name)
        target.parent.mkdir()
        target.write_bytes(file.content)
        return target.relative_to(directory).as_posix()

    for name, info in request.signature.input_model.model_fields.items():
        value = getattr(request.inputs, name)
        if info.annotation is Workspace:
            values[name] = "the current working directory"
        elif info.annotation is File:
            values[name] = write(value)
        elif info.annotation == list[File]:
            values[name] = [write(item) for item in value]
        else:
            values[name] = to_jsonable_python(value)
    return values


def _file_name(name: str | None) -> str:
    """The file's own base name, or ``file`` when the filesystem cannot use it."""
    base = Path(name or "").name
    if base in {"", ".", ".."} or "\x00" in base or len(base.encode()) > 255:
        return "file"
    return base


def _prompt(inputs: Mapping[str, JsonValue]) -> str:
    return (
        "Complete the task for these inputs. Files are given as paths relative to the "
        "current working directory.\n\n" + json.dumps(inputs, indent=2, ensure_ascii=False)
    )


def _system_prompt(instructions: str, append: str | None) -> str:
    prompt = instructions.strip()
    return f"{prompt}\n\n{append.strip()}" if append else prompt


def _mentions(annotation: object, target: type, seen: frozenset[type] = frozenset()) -> bool:
    """Whether ``target`` appears anywhere in a field type, including inside models."""
    if annotation is target:
        return True
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return False
        return any(
            _mentions(field.annotation, target, seen | {annotation})
            for field in annotation.model_fields.values()
        )
    origin = get_origin(annotation)
    if origin is None:
        return False
    if origin is Annotated:
        return _mentions(get_args(annotation)[0], target, seen)
    if origin in (Union, types.UnionType) or isinstance(origin, type):
        return any(_mentions(argument, target, seen) for argument in get_args(annotation))
    return False


def _check_inputs(request: AgentRequest) -> None:
    """Files and workspaces are supported only as whole input field types."""
    for name, info in request.signature.input_model.model_fields.items():
        annotation = info.annotation
        if annotation in (File, Workspace, list[File]):
            continue
        if _mentions(annotation, File) or _mentions(annotation, Workspace):
            raise AgentStepError(
                f"agent step {request.step_name!r}: ClaudeAgent input {name!r} must be typed "
                "exactly ava.File, list[ava.File] or ava.Workspace when it holds files"
            )


def _result_errors(result: ResultMessage) -> tuple[str, ...]:
    """Why a result failed; API failures carry their reason only in the result text."""
    if result.errors:
        return tuple(result.errors)
    if result.is_error and result.result:
        return (result.result[:_MAX_ERROR_CHARS],)
    return ()


def _output_schema(model: type[BaseModel], workspace_outputs: list[str]) -> dict[str, Any]:
    """The signature's output schema, without the fields Avalanche fills itself."""
    schema = model.model_json_schema()
    for name in workspace_outputs:
        del schema["properties"][name]
        schema["required"] = [field for field in schema.get("required", ()) if field != name]
    return schema


def _tool_server(tools: tuple[ToolSpec, ...]) -> Any:
    """The step's Python tools as an in-process MCP server."""
    return create_sdk_mcp_server(_SERVER, tools=[_sdk_tool(spec) for spec in tools])


def _sdk_tool(spec: ToolSpec) -> Any:
    arguments = _arguments_model(spec)

    async def handler(raw: dict[str, Any]) -> dict[str, Any]:
        # A failing tool is reported to the model, which can recover; the trace keeps it.
        try:
            parsed = arguments.model_validate(raw)
            result = spec.function(
                **{name: getattr(parsed, name) for name in arguments.model_fields}
            )
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": to_json(result).decode()}]}

    return tool(spec.name, spec.description or "", arguments.model_json_schema())(handler)


def _arguments_model(spec: ToolSpec) -> type[BaseModel]:
    function: Callable[..., object] = spec.function
    hints = get_type_hints(function)
    fields: dict[str, Any] = {}
    for name, parameter in inspect.signature(function).parameters.items():
        if name not in hints:
            raise AgentStepError(
                f"tool {spec.name!r} parameter {name!r} needs a type annotation"
            )
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[name] = (hints[name], default)
    return create_model(
        f"{spec.name}Arguments", __config__=ConfigDict(extra="forbid"), **fields
    )


class _SdkUsage(BaseModel):
    """The token counts the SDK reports as a loose dictionary."""

    model_config = ConfigDict(extra="ignore", strict=True)

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


class _SystemInit(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    session_id: str | None = None
    model: str | None = None
    tools: list[str] = []


def _usage(raw: Mapping[str, Any] | None, cost: float | None = None) -> TokenUsage | None:
    if raw is None:
        return None
    usage = _SdkUsage.model_validate(raw)
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cost_usd=cost,
    )


def _bounded(value: object) -> JsonValue:
    """A JSON value for the trace, replaced by a marker when it is too large."""
    projected = _JSON.validate_python(to_jsonable_python(value))
    if len(to_json(projected)) <= _MAX_VALUE_BYTES:
        return projected
    return {"kind": "unavailable", "reason": "value exceeds the trace size limit"}


def _within_budget(actions: tuple[ToolCallAction, ...]) -> tuple[ToolCallAction, ...]:
    """The turn's first tool calls that fit its live-event budget, in order."""
    kept: list[ToolCallAction] = []
    used = 0
    for action in actions:
        used += len(action.model_dump_json().encode())
        if used > _MAX_TURN_ACTION_BYTES:
            break
        kept.append(action)
    return tuple(kept)


def _structured_output(value: object) -> dict[str, JsonValue]:
    """The result's structured output for the trace, or a note that it was too large."""
    projected = _JSON.validate_python(to_jsonable_python(value))
    if len(to_json(projected)) <= _MAX_VALUE_BYTES:
        return {"structured_output": projected, "output_omitted": False}
    return {"structured_output": None, "output_omitted": True}


def _result_text(content: str | list[dict[str, Any]] | None) -> JsonValue:
    return _bounded(content)


@dataclass
class _PendingTurn:
    """One assistant response waiting for the results of the tools it called.

    The CLI may stream one response as several messages sharing a message ID;
    their blocks are gathered here in order.
    """

    message: AssistantMessage
    blocks: list[object] = field(default_factory=list)
    calls: dict[str, ToolCallAction] = field(default_factory=dict)


class _Transcript:
    """Turns the SDK's message stream into live events and a trace record."""

    def __init__(self, events: AgentEventSink) -> None:
        self._events = events
        self._pending: _PendingTurn | None = None
        self.turns: list[Turn] = []
        self.session_id: str | None = None
        self.model: str | None = None
        self.result: ResultMessage | None = None

    async def add(self, message: object) -> None:
        match message:
            case SystemMessage(subtype="init", data=data):
                init = _SystemInit.model_validate(data)
                self.session_id = init.session_id
                await self._events.emit(
                    native.SessionInitialized(
                        session_id=init.session_id, model=init.model, tools=tuple(init.tools)
                    ),
                    None,
                )
            case (
                AssistantMessage(parent_tool_use_id=str())
                | UserMessage(parent_tool_use_id=str())
            ):
                # A subagent's own messages: its work returns to this session as the
                # result of the tool call that started it.
                pass
            case AssistantMessage():
                pending = self._pending
                if (
                    pending is None
                    or message.message_id is None
                    or message.message_id != pending.message.message_id
                ):
                    await self._finish_turn()
                    self.model = self.model or message.model
                    pending = self._pending = _PendingTurn(message)
                else:
                    # The same response continued: its latest message holds the usage.
                    pending.message = message
                pending.blocks.extend(message.content)
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        pending.calls[block.id] = ToolCallAction(
                            call_id=block.id,
                            caller="model",
                            name=block.name,
                            arguments=_bounded(block.input),
                            result=None,
                            error=None,
                            duration_ms=None,
                        )
                        await self._events.emit(
                            native.ToolUsed(turn_index=len(self.turns) + 1),
                            ToolStarted(call_id=block.id, name=block.name),
                        )
            case UserMessage(content=list() as blocks):
                for block in blocks:
                    if isinstance(block, ToolResultBlock):
                        await self._tool_result(block)
            case RateLimitEvent(rate_limit_info=info):
                await self._events.emit(
                    native.RateLimited(status=info.status, limit_type=info.rate_limit_type),
                    None,
                )
            case ResultMessage():
                await self._finish_turn()
                self.result = message
                self.session_id = message.session_id
                await self._events.emit(
                    native.Finished(
                        subtype=message.subtype,
                        is_error=message.is_error,
                        num_turns=message.num_turns,
                        **_structured_output(message.structured_output),
                        errors=_result_errors(message),
                    ),
                    None,
                )
            # Other system notices, prompt echoes and stream deltas carry nothing the
            # trace keeps.

    async def _tool_result(self, block: ToolResultBlock) -> None:
        pending = self._pending
        if pending is None or block.tool_use_id not in pending.calls:
            raise AgentStepExecutionError(
                f"Claude returned a result for unknown tool call {block.tool_use_id!r}"
            )
        call = pending.calls[block.tool_use_id]
        content = _result_text(block.content)
        error = None
        if block.is_error:
            error = content if isinstance(content, str) else json.dumps(content)
        pending.calls[block.tool_use_id] = call.model_copy(
            update={"result": None if error is not None else content, "error": error}
        )
        await self._events.emit(
            native.ToolReturned(turn_index=len(self.turns) + 1),
            ToolFinished(call_id=call.call_id, name=call.name, error=error),
        )

    async def _finish_turn(self) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        message = pending.message
        text = "".join(block.text for block in pending.blocks if isinstance(block, TextBlock))
        reasoning = "".join(
            block.thinking for block in pending.blocks if isinstance(block, ThinkingBlock)
        )
        turn = Turn(
            index=len(self.turns) + 1,
            text=text or None,
            reasoning=reasoning or None,
            actions=tuple(pending.calls.values()),
            duration_ms=None,
            usage=_usage(message.usage),
        )
        self.turns.append(turn)
        # The record keeps every call; the live event keeps those within its budget.
        kept = _within_budget(turn.actions)
        await self._events.emit(
            native.MessageFinished(
                message_id=message.message_id,
                model=message.model,
                stop_reason=message.stop_reason,
                api_error=message.error,
            ),
            TurnFinished(
                turn=turn.model_copy(update={"actions": kept}),
                omitted_tool_calls=len(turn.actions) - len(kept),
                omitted_model_calls=0,
            ),
        )

    def record(self, result: ResultMessage, *, model: str | None) -> AgentTrace:
        succeeded = result.subtype == "success" and not result.is_error
        # Outputs are an object of fields; anything else fails the step, so the trace too.
        has_output = isinstance(result.structured_output, dict)
        status: AgentTraceStatus = "succeeded" if succeeded and has_output else "failed"
        return AgentTrace(
            header=AgentTraceHeader(
                runtime_kind="claude_agent",
                trace_format=native.FORMAT,
                status=status,
                stop_reason=result.subtype
                if has_output or not succeeded
                else "no_structured_output"
                if result.structured_output is None
                else "structured_output_not_object",
                model=self.model or model,
                duration_ms=result.duration_ms,
                usage=_usage(result.usage, result.total_cost_usd),
                turn_count=len(self.turns),
                tool_call_count=sum(len(turn.actions) for turn in self.turns),
                external_run_id=result.session_id,
                session_id=result.session_id,
            ),
            turns=tuple(self.turns),
            native=ClaudeAgentNativeTrace(
                result_subtype=result.subtype,
                num_turns=result.num_turns,
                duration_api_ms=result.duration_api_ms,
                permission_denials=len(result.permission_denials or ()),
                errors=_result_errors(result),
            ),
            turns_streamed=False,
        )
