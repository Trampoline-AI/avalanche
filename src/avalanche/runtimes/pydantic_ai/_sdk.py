"""Run one agent call through Pydantic AI; the only module importing Pydantic AI."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import mimetypes
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pydantic_ai
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError
from pydantic import create_model as create_pydantic_model
from pydantic_ai import Agent, Tool
from pydantic_ai.capabilities import Capability
from pydantic_ai.exceptions import AgentRunError, ToolFailed
from pydantic_ai.messages import (
    BaseToolCallPart,
    BaseToolReturnPart,
    BinaryContent,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallEvent,
    ToolResultEvent,
    UserContent,
)
from pydantic_ai.run import AgentRun
from pydantic_ai.usage import UsageBase, UsageLimits
from pydantic_core import to_json, to_jsonable_python

from ...agent.contract import (
    AgentEventSink,
    AgentOutputs,
    AgentRequest,
    AgentRunOutcome,
    PythonOutputs,
    ToolSpec,
)
from ...agent.live import RunStarted, ToolFinished, ToolStarted, TurnFinished
from ...agent.native import pydantic_ai as native
from ...agent.skills import Skill
from ...agent.steps import AgentOutputError, AgentStepError, AgentStepExecutionError
from ...agent.trace import (
    AgentTrace,
    AgentTraceHeader,
    AgentTraceStatus,
    TokenUsage,
    ToolCall,
    Turn,
    evidence_value,
    retained_fields,
)
from ...file import File
from ...references import AgentOutputFolder
from ...workspace import Workspace
from .._file_fields import FileFields
from .runtime import agent_option_names, unknown_agent_options

if TYPE_CHECKING:
    from .runtime import PydanticAIAgent

# Avalanche owns the terminal, which may be running the TUI: Pydantic AI's first-run
# banner must not print into it.
pydantic_ai.BANNER_ENABLED = False

_JSON = TypeAdapter(JsonValue)
# Tool arguments and results kept in a trace; larger values are replaced by a marker.
_MAX_VALUE_BYTES = 256 * 1024
# A turn's tool calls kept in its live event; later calls are counted as omitted, so the
# event stays well under the operator's 8 MiB agent-event limit.
_MAX_TURN_TOOL_CALL_BYTES = 4 * 1024 * 1024
# A failed run's error, kept in the trace header that every run snapshot carries.
_MAX_ERROR_CHARS = 4096
# Files list_files returns at most; a larger folder must be listed in parts.
_MAX_LISTED_FILES = 1000
# Agent arguments Avalanche fills that are combined with the user's.
_COMBINED_OPTIONS = ("tools", "toolsets", "capabilities")
# The stop reason of a run that returned its answer.
_FINAL_RESULT = "final_result"


async def run(
    runtime: PydanticAIAgent, request: AgentRequest, events: AgentEventSink
) -> AgentRunOutcome:
    signature = request.signature
    fields = FileFields.of(request, runtime="PydanticAIAgent")
    if runtime.model is None:
        raise AgentStepError(
            f"agent step {request.step_name!r}: PydanticAIAgent needs a model, such as "
            "PydanticAIAgent(model='openai:gpt-5.4-nano'), on the step or as the workflow "
            "default"
        )
    options = _agent_options(runtime.agent_options, step_name=request.step_name)
    prompt = _prompt(request)
    workspace: Workspace | None = None
    with contextlib.ExitStack() as stack:
        directory: Path | None = None
        if fields.workspace_input is not None:
            workspace = getattr(request.inputs, fields.workspace_input)
            # A reference workspace runs in place, so the agent's writes land in the
            # host's directory; a value workspace runs in its temporary copy.
            directory = workspace.path
        elif fields.workspace_outputs:
            directory = Path(
                stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="avalanche-pydantic-ai-")
                )
            )
        # File outputs go to a fresh folder in the run workspace, as with every runtime.
        output_folder = AgentOutputFolder(request.step_name) if fields.file_outputs else None
        tools = [
            *(_step_tool(spec) for spec in request.tools),
            *(_workspace_tools(directory) if directory is not None else ()),
            *(
                (_skill_file_tool(request.skills),)
                if any(skill.files for skill in request.skills)
                else ()
            ),
        ]
        inputs, omitted_inputs = retained_fields(
            {name: getattr(request.inputs, name) for name in signature.input_model.model_fields}
        )
        await events.emit(
            None,
            RunStarted(model=runtime.model, inputs=inputs, omitted_inputs=omitted_inputs),
        )
        transcript = _Transcript(events)
        started = time.monotonic()
        try:
            agent = Agent(
                runtime.model,
                output_type=_answer_model(signature.output_model, fields),
                instructions=_instructions(
                    signature.instructions,
                    runtime.instructions_append,
                    workspace=directory is not None,
                ),
                **{
                    "name": request.step_name,
                    **options,
                    "tools": [*tools, *options.get("tools", ())],
                    "toolsets": list(options.get("toolsets", ())),
                    "capabilities": [
                        *(_skill_capability(skill) for skill in request.skills),
                        *options.get("capabilities", ()),
                    ],
                },
            )
            async with agent.iter(
                prompt,
                usage_limits=(
                    UsageLimits(request_limit=runtime.max_turns)
                    if runtime.max_turns is not None
                    else None
                ),
            ) as agent_run:
                await transcript.start(
                    agent_run, model=runtime.model, tools=tools, skills=request.skills
                )
                async for node in agent_run:
                    if Agent.is_model_request_node(node):
                        transcript.request_started()
                    elif Agent.is_call_tools_node(node):
                        await transcript.response(node.model_response)
                        async with node.stream(agent_run.ctx) as stream:
                            async for event in stream:
                                await transcript.event(event)
                        await transcript.finish_turn()
            result = agent_run.result
        except asyncio.CancelledError:
            events.unavailable(
                status="cancelled",
                error="the agent call was cancelled",
                external_run_id=transcript.run_id,
                complete=True,
            )
            raise
        except AgentRunError as exc:
            # Pydantic AI ended the run itself: a model API error, a usage limit, or
            # answers that kept failing validation. The turns so far form its trace.
            await transcript.finish_turn()
            record = transcript.record(
                status="failed",
                stop_reason=type(exc).__name__,
                error=str(exc)[:_MAX_ERROR_CHARS],
                duration_ms=_elapsed_ms(started),
                model=runtime.model,
            )
            events.finished(record.header, outputs=None, complete=True)
            raise AgentStepExecutionError(
                f"agent step {request.step_name!r}: Pydantic AI did not finish: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        except Exception as exc:
            events.unavailable(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                external_run_id=transcript.run_id,
                complete=True,
            )
            raise AgentStepExecutionError(
                f"agent step {request.step_name!r} failed calling {runtime.model}: {exc}"
            ) from exc
        if result is None:
            events.unavailable(
                status="failed",
                error="the Pydantic AI run ended without a result",
                external_run_id=transcript.run_id,
                complete=True,
            )
            raise AgentStepExecutionError(
                f"agent step {request.step_name!r}: the Pydantic AI run ended without a result"
            )
        record = transcript.record(
            status="succeeded",
            stop_reason=_FINAL_RESULT,
            error=None,
            duration_ms=_elapsed_ms(started),
            model=runtime.model,
        )
        try:
            outputs = _outputs(
                request,
                result.output,
                fields=fields,
                workspace=workspace,
                directory=directory,
                output_folder=output_folder,
            )
        except Exception:
            # The step fails, and its final event says the run returned no outputs.
            events.finished(record.header, outputs=None, complete=True)
            raise
        events.finished(record.header, outputs=outputs, complete=True)
        return AgentRunOutcome(outputs=outputs, trace=record)


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _agent_options(given: Mapping[str, Any], *, step_name: str) -> Mapping[str, Any]:
    """The ``pydantic_ai.Agent`` arguments the user gave, checked by name and shape."""
    if problem := unknown_agent_options(given.keys(), agent_option_names(pydantic_ai.Agent)):
        raise AgentStepError(f"agent step {step_name!r}: {problem}")
    for name in _COMBINED_OPTIONS:
        if name in given and (
            isinstance(given[name], str) or not isinstance(given[name], Sequence)
        ):
            raise AgentStepError(
                f"agent step {step_name!r}: PydanticAIAgent {name} must be a list, which "
                "Avalanche combines with its own entries for the step"
            )
    return given


def _instructions(instructions: str, append: str | None, *, workspace: bool) -> str:
    parts = [instructions.strip()]
    if workspace:
        parts.append(
            "You work in a folder of files. Use list_files, read_file and write_file to "
            "look at and change them; paths are relative to that folder."
        )
    if append:
        parts.append(append.strip())
    return "\n\n".join(parts)


# Pydantic AI hands files to the model in these forms; each provider takes some of them.
def _sendable(content: BinaryContent) -> bool:
    return content.is_image or content.is_audio or content.is_video or content.is_document


def _attachment(request: AgentRequest, name: str, file: File) -> BinaryContent:
    media_type = file.content_type or mimetypes.guess_type(file.name or file.path.name)[0]
    if media_type is None:
        raise AgentStepError(
            f"agent step {request.step_name!r}: PydanticAIAgent input {name!r} has a file "
            f"without a content type ({file.name or file.path.name}); give it one with "
            "ava.File.from_path(..., content_type=...)"
        )
    content = BinaryContent(file.read_bytes(), media_type=media_type)
    if not _sendable(content):
        raise AgentStepError(
            f"agent step {request.step_name!r}: PydanticAIAgent input {name!r} is a "
            f"{media_type} file; Pydantic AI sends only images, audio, video and documents "
            "(PDF, plain text, CSV, HTML, Markdown, Word and Excel) to a model"
        )
    return content


def _prompt(request: AgentRequest) -> list[UserContent]:
    """The inputs as JSON, followed by each input file as an attachment it names."""
    values: dict[str, JsonValue] = {}
    attachments: list[tuple[str, BinaryContent]] = []

    def attach(name: str, file: File) -> str:
        label = f"attached file {len(attachments) + 1}"
        attachments.append(
            (f"{label}: {file.name or file.path.name}", _attachment(request, name, file))
        )
        return label

    for name, info in request.signature.input_model.model_fields.items():
        value = getattr(request.inputs, name)
        if info.annotation is Workspace:
            values[name] = "the folder the file tools work in"
        elif info.annotation is File:
            values[name] = attach(name, value)
        elif info.annotation == list[File]:
            values[name] = [attach(name, item) for item in value]
        else:
            values[name] = to_jsonable_python(value)
    prompt: list[UserContent] = [
        "Complete the task for these inputs. Input files are attached after them, each "
        "named by its label.\n\n" + json.dumps(values, indent=2, ensure_ascii=False)
    ]
    for label, content in attachments:
        prompt.extend((label, content))
    return prompt


class _TextFile(BaseModel):
    """A text file returned as an output."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="The file's name, such as report.md; a relative path such as "
        "data/table.csv makes folders."
    )
    text: str = Field(description="The file's full text.")


def _answer_model(output_model: type[BaseModel], fields: FileFields) -> type[BaseModel]:
    """The signature's outputs as the model answers them.

    Avalanche fills workspace outputs, and the model writes file outputs as text files.
    """
    definitions: dict[str, Any] = {}
    for name, info in output_model.model_fields.items():
        if name in fields.workspace_outputs:
            continue
        if name in fields.file_outputs:
            annotation = _TextFile if fields.file_outputs[name] else list[_TextFile]
            definitions[name] = (annotation, Field(description=info.description))
        else:
            definitions[name] = (info.annotation, info)
    return create_pydantic_model(output_model.__name__.removesuffix("Outputs"), **definitions)


def _outputs(
    request: AgentRequest,
    answer: BaseModel,
    *,
    fields: FileFields,
    workspace: Workspace | None,
    directory: Path | None,
    output_folder: AgentOutputFolder | None,
) -> AgentOutputs:
    """The model's answer, with the workspaces and files Avalanche returns."""
    values: dict[str, object] = {
        name: getattr(answer, name)
        for name in type(answer).model_fields
        if name not in fields.file_outputs
    }
    if output_folder is not None:
        values.update(_output_files(request, answer, fields.file_outputs, output_folder))
    if fields.workspace_outputs:
        assert directory is not None, "a workspace output has a working folder"
        snapshot = (
            workspace.snapshot() if workspace is not None else Workspace.from_path(directory)
        )
        values.update(dict.fromkeys(fields.workspace_outputs, snapshot))
    return PythonOutputs(values=values)


def _output_files(
    request: AgentRequest,
    answer: BaseModel,
    file_outputs: Mapping[str, bool],
    output_folder: AgentOutputFolder,
) -> dict[str, File | list[File]]:
    """The model's text files, written into the output folder as file references."""
    root = output_folder.path.resolve()
    written: set[Path] = set()

    def write(name: str, text_file: _TextFile) -> File:
        target = (root / text_file.name).resolve()
        if not target.is_relative_to(root) or target == root:
            raise AgentOutputError(
                f"agent step {request.step_name!r} output {name!r}: file name "
                f"{text_file.name!r} must stay inside the output folder"
            )
        if target in written:
            raise AgentOutputError(
                f"agent step {request.step_name!r} output {name!r}: the answer names "
                f"{text_file.name!r} more than once"
            )
        written.add(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text_file.text, encoding="utf-8")
        return output_folder.file(target)

    files: dict[str, File | list[File]] = {}
    for name, single in file_outputs.items():
        value = getattr(answer, name)
        files[name] = write(name, value) if single else [write(name, item) for item in value]
    return files


def _failing_as_tool_errors(function: Callable[..., object]) -> Callable[..., object]:
    """``function`` reporting its exceptions to the model, which can recover."""
    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def call_async(*args: object, **kwargs: object) -> object:
            try:
                return await function(*args, **kwargs)
            except Exception as exc:
                raise ToolFailed(f"{type(exc).__name__}: {exc}") from exc

        return call_async

    @functools.wraps(function)
    def call(*args: object, **kwargs: object) -> object:
        try:
            result = function(*args, **kwargs)
        except Exception as exc:
            raise ToolFailed(f"{type(exc).__name__}: {exc}") from exc
        if inspect.isawaitable(result):
            raise AgentStepError(
                f"tool {getattr(function, '__name__', function)!r} returned an awaitable "
                "from a plain function; declare it with async def"
            )
        return result

    return call


def _step_tool(spec: ToolSpec) -> Tool[None]:
    return Tool(
        _failing_as_tool_errors(spec.function),
        name=spec.name,
        description=spec.description,
        takes_ctx=False,
    )


def _workspace_tools(directory: Path) -> tuple[Tool[None], ...]:
    """Text file tools for the folder the agent works in; no path leaves it."""
    root = directory.resolve()

    def inside(path: str) -> Path:
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ToolFailed(f"{path!r} is outside the working folder")
        return target

    def list_files(folder: str = ".") -> list[str]:
        """List the files under a folder of the working folder, as paths relative to it."""
        base = inside(folder)
        if not base.is_dir():
            raise ToolFailed(f"{folder!r} is not a folder")
        files = sorted(
            entry.relative_to(root).as_posix() for entry in base.rglob("*") if entry.is_file()
        )
        if len(files) > _MAX_LISTED_FILES:
            raise ToolFailed(
                f"{folder!r} holds {len(files)} files, more than {_MAX_LISTED_FILES}; list "
                "one of its folders instead"
            )
        return files

    def read_file(path: str) -> str:
        """Read a UTF-8 text file in the working folder."""
        try:
            return inside(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ToolFailed(f"cannot read {path!r}: {exc}") from exc

    def write_file(path: str, text: str) -> str:
        """Write a UTF-8 text file in the working folder, replacing it if it exists."""
        target = inside(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise ToolFailed(f"cannot write {path!r}: {exc}") from exc
        return f"wrote {path}"

    return tuple(Tool(tool, takes_ctx=False) for tool in (list_files, read_file, write_file))


def _skill_capability(skill: Skill) -> Capability[None]:
    """An Agent Skill as a capability the model loads when its description fits the task."""
    instructions = skill.instructions
    if skill.files:
        instructions += (
            f"\n\nRead this skill's files with read_skill_file, skill {skill.name!r}:\n"
            + "\n".join(f"- {path}" for path in sorted(skill.files))
        )
    return Capability(
        id=skill.name,
        description=skill.description,
        instructions=instructions,
        defer_loading=True,
    )


def _skill_file_tool(skills: tuple[Skill, ...]) -> Tool[None]:
    files = {skill.name: skill.files for skill in skills}

    def read_skill_file(skill: str, path: str) -> str:
        """Read a UTF-8 text file of a loaded Agent Skill, by the skill's name and the
        file's path in the skill."""
        host = files.get(skill, {}).get(path)
        if host is None:
            raise ToolFailed(f"skill {skill!r} has no file {path!r}")
        try:
            return host.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ToolFailed(f"cannot read {path!r} of skill {skill!r}: {exc}") from exc

    return Tool(read_skill_file, takes_ctx=False)


def _usage(usage: UsageBase) -> TokenUsage | None:
    """Pydantic AI's token counts; its input count includes the prompt cache, ours not."""
    if usage.unmeasured_requests:
        return None
    return TokenUsage(
        input_tokens=usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_tokens,
        cache_creation_input_tokens=usage.cache_write_tokens,
        cost_usd=float(usage.cost) if usage.cost is not None else None,
    )


def _bounded(value: JsonValue) -> JsonValue:
    """A JSON value for the trace, replaced by a marker when it is too large."""
    if len(to_json(value)) <= _MAX_VALUE_BYTES:
        return value
    return {"kind": "unavailable", "reason": "value exceeds the trace size limit"}


def _arguments(part: BaseToolCallPart) -> JsonValue:
    """A call's arguments; a model may write them as text that is not JSON."""
    if isinstance(part.args, str):
        try:
            return _bounded(_JSON.validate_json(part.args))
        except ValidationError:
            return _bounded(part.args)
    return _bounded(evidence_value(part.args or {}))


def _within_budget(calls: tuple[ToolCall, ...]) -> tuple[ToolCall, ...]:
    """The turn's first tool calls that fit its live-event budget, in order."""
    kept: list[ToolCall] = []
    used = 0
    for call in calls:
        used += len(call.model_dump_json().encode())
        if used > _MAX_TURN_TOOL_CALL_BYTES:
            break
        kept.append(call)
    return tuple(kept)


@dataclass
class _PendingTurn:
    """One model response waiting for the results of the tools it called."""

    response: ModelResponse
    started: float
    calls: dict[str, ToolCall]
    call_started: dict[str, float] = field(default_factory=dict)


class _Transcript:
    """Turns Pydantic AI's run into live events and a trace record."""

    def __init__(self, events: AgentEventSink) -> None:
        self._events = events
        self._pending: _PendingTurn | None = None
        self._request_started = time.monotonic()
        self._run: AgentRun[None, Any] | None = None
        self.turns: list[Turn] = []
        self.retries = 0
        self.run_id: str | None = None
        self.model: str | None = None

    async def start(
        self,
        agent_run: AgentRun[None, Any],
        *,
        model: str,
        tools: Sequence[Tool[None]],
        skills: tuple[Skill, ...],
    ) -> None:
        self._run = agent_run
        self.run_id = agent_run.run_id
        await self._events.emit(
            native.RunInitialized(
                run_id=agent_run.run_id,
                conversation_id=agent_run.conversation_id,
                model=model,
                tools=tuple(tool.name for tool in tools),
                skills=tuple(skill.name for skill in skills),
            ),
            None,
        )

    def request_started(self) -> None:
        self._request_started = time.monotonic()

    async def response(self, response: ModelResponse) -> None:
        self.model = self.model or response.model_name
        calls: dict[str, ToolCall] = {}
        returns = {
            part.tool_call_id: part
            for part in response.parts
            if isinstance(part, BaseToolReturnPart)
        }
        for part in response.parts:
            if not isinstance(part, BaseToolCallPart):
                continue
            # A tool the provider ran itself returns in the same response.
            provided = returns.get(part.tool_call_id)
            calls[part.tool_call_id] = ToolCall(
                call_id=part.tool_call_id,
                caller="model",
                name=part.tool_name,
                arguments=_arguments(part),
                result=None if provided is None else _result(provided),
                error=None if provided is None else _error(provided),
                duration_ms=None,
            )
        self._pending = _PendingTurn(response, self._request_started, calls)

    async def event(self, event: object) -> None:
        pending = self._pending
        assert pending is not None, "tool events follow the response that called them"
        turn = len(self.turns) + 1
        match event:
            case ToolCallEvent(part=part):
                pending.call_started[part.tool_call_id] = time.monotonic()
                await self._events.emit(
                    None, ToolStarted(call_id=part.tool_call_id, name=part.tool_name, turn=turn)
                )
            case ToolResultEvent(part=part):
                call = pending.calls.get(part.tool_call_id)
                if call is None:
                    raise AgentStepExecutionError(
                        f"Pydantic AI returned a result for unknown tool call "
                        f"{part.tool_call_id!r}"
                    )
                started = pending.call_started.get(part.tool_call_id)
                error = _error(part)
                pending.calls[part.tool_call_id] = call.model_copy(
                    update={
                        "result": _result(part) if error is None else None,
                        "error": error,
                        "duration_ms": None if started is None else _elapsed_ms(started),
                    }
                )
                retried = None
                if part.outcome == "retried" and part.retry is not None:
                    self.retries += 1
                    retried = native.ToolRetried(
                        call_id=part.tool_call_id,
                        attempt=part.retry.attempt,
                        max_retries=part.retry.max_retries,
                    )
                await self._events.emit(
                    retried,
                    ToolFinished(
                        call_id=part.tool_call_id, name=part.tool_name, turn=turn, error=error
                    ),
                )
            # Text, thinking and tool-availability deltas reach the trace through the
            # response itself.

    async def finish_turn(self) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        response = pending.response
        text = "\n\n".join(
            part.content
            for part in response.parts
            if isinstance(part, TextPart) and part.content
        )
        reasoning = "\n\n".join(
            part.content
            for part in response.parts
            if isinstance(part, ThinkingPart) and part.content
        )
        turn = Turn(
            index=len(self.turns) + 1,
            message=text or None,
            reasoning=reasoning or None,
            tool_calls=tuple(pending.calls.values()),
            duration_ms=_elapsed_ms(pending.started),
            usage=_usage(response.usage),
            native=native.PydanticAITurn(
                model=response.model_name,
                provider=response.provider_name,
                provider_response_id=response.provider_response_id,
                finish_reason=response.finish_reason,
            ),
        )
        self.turns.append(turn)
        # The record keeps every call; the live event keeps those within its budget.
        kept = _within_budget(turn.tool_calls)
        await self._events.emit(
            None,
            TurnFinished(
                turn=turn.model_copy(update={"tool_calls": kept}),
                omitted_tool_calls=len(turn.tool_calls) - len(kept),
            ),
        )

    def record(
        self,
        *,
        status: AgentTraceStatus,
        stop_reason: str,
        error: str | None,
        duration_ms: int,
        model: str,
    ) -> AgentTrace:
        usage = self._run.usage if self._run is not None else None
        return AgentTrace(
            header=AgentTraceHeader(
                status=status,
                stop_reason=stop_reason,
                model=self.model or model,
                duration_ms=duration_ms,
                usage=None if usage is None else _usage(usage),
                turn_count=len(self.turns),
                tool_call_count=sum(len(turn.tool_calls) for turn in self.turns),
                external_run_id=self.run_id,
                session_id=None if self._run is None else self._run.conversation_id,
                native=native.PydanticAITraceHeader(
                    requests=0 if usage is None else usage.requests,
                    retries=self.retries,
                    error=error,
                ),
            ),
            turns=tuple(self.turns),
        )


def _error(part: BaseToolReturnPart) -> str | None:
    """Why a tool call did not succeed, as the model was told; ``None`` when it did."""
    if part.outcome == "success":
        return None
    return part.model_response_str()


def _result(part: BaseToolReturnPart) -> JsonValue:
    return _bounded(evidence_value(part.content))
