"""Run one agent call through the OpenAI Agents SDK; the only module importing the SDK."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import functools
import inspect
import json
import mimetypes
import re
import time
import types
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin

from agents import (
    Agent,
    AgentOutputSchemaBase,
    AgentUpdatedStreamEvent,
    FunctionTool,
    ModelBehaviorError,
    ModelSettings,
    RawResponsesStreamEvent,
    RunConfig,
    RunContextWrapper,
    RunItemStreamEvent,
    Runner,
    StreamEvent,
    ToolCallItem,
    ToolCallOutputItem,
    Usage,
    UserError,
    function_tool,
    gen_trace_id,
)
from agents.models.interface import ModelProvider
from agents.models.openai_provider import OpenAIProvider
from agents.strict_schema import ensure_strict_json_schema
from agents.tool_context import ToolContext
from openai import AsyncOpenAI, OpenAIError
from openai.types.responses import (
    EasyInputMessageParam,
    Response,
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseFunctionToolCall,
    ResponseInputFileParam,
    ResponseInputImageParam,
    ResponseInputItemParam,
    ResponseInputTextParam,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseReasoningItem,
    ResponseUsage,
)
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError
from pydantic_core import to_json, to_jsonable_python

from ...agent.contract import (
    AgentEventSink,
    AgentOutputs,
    AgentRequest,
    AgentRunOutcome,
    JsonOutputs,
    PythonOutputs,
    ToolSpec,
)
from ...agent.live import RunStarted, ToolFinished, ToolStarted, TurnFinished
from ...agent.native import openai_agent as native
from ...agent.skills import Skill
from ...agent.steps import AgentOutputError, AgentStepError, AgentStepExecutionError
from ...agent.trace import (
    AgentTrace,
    AgentTraceHeader,
    AgentTraceStatus,
    TokenUsage,
    ToolCall,
    Turn,
    retained_fields,
)
from ...file import File
from ...references import AgentOutputFolder
from ...workspace import Workspace
from .runtime import DEFAULT_MAX_TURNS, unknown_model_settings

if TYPE_CHECKING:
    from .runtime import OpenAIAgent

_JSON = TypeAdapter(JsonValue)
_ANSWER = TypeAdapter(dict[str, JsonValue])
# Tool arguments and results kept in a trace; larger values are replaced by a marker.
_MAX_VALUE_BYTES = 256 * 1024
# A turn's tool calls kept in its live event; later calls are counted as omitted, so the
# event stays well under the operator's 8 MiB agent-event limit.
_MAX_TURN_TOOL_CALL_BYTES = 4 * 1024 * 1024
# The error kept in the trace header, which every run snapshot carries: even fully
# JSON-escaped (6 bytes a character) it stays under the header's 64 KiB limit.
_MAX_ERROR_CHARS = 4096
# The tools that offer the step's Agent Skills to the model.
_LOAD_SKILL = "load_skill"
_READ_SKILL_FILE = "read_skill_file"


async def run(
    runtime: OpenAIAgent, request: AgentRequest, events: AgentEventSink
) -> AgentRunOutcome:
    signature = request.signature
    _check_fields(request)
    file_outputs = {
        name: info.annotation is File
        for name, info in signature.output_model.model_fields.items()
        if info.annotation in (File, list[File])
    }
    if runtime.narrate and _SUMMARY_FIELD in signature.output_model.model_fields:
        raise AgentStepError(
            f"agent step {request.step_name!r}: with narrate on, OpenAIAgent asks the model "
            f"for its own {_SUMMARY_FIELD!r} answer field; rename the signature's output or "
            "set narrate=False"
        )
    if request.skills:
        reserved = sorted(
            {_LOAD_SKILL, _READ_SKILL_FILE} & {spec.name for spec in request.tools}
        )
        if reserved:
            raise AgentStepError(
                f"agent step {request.step_name!r}: the tool names {reserved} are reserved "
                "for the step's skills; rename the step's tools"
            )
    if problem := unknown_model_settings(
        runtime.model_settings.keys(),
        {option.name for option in dataclasses.fields(ModelSettings)},
    ):
        raise AgentStepError(f"agent step {request.step_name!r}: {problem}")
    tool_errors: dict[str, str] = {}
    try:
        tools = [
            *(_step_tool(spec, tool_errors) for spec in request.tools),
            *(_skill_tools(request.skills, tool_errors) if request.skills else ()),
        ]
        answer_schema = _AnswerSchema(
            _output_schema(signature.output_model, file_outputs, summary=runtime.narrate)
        )
    except UserError as exc:
        raise AgentStepError(
            f"agent step {request.step_name!r}: OpenAIAgent sends tool arguments and answers "
            f"as OpenAI strict JSON schemas, which cannot express this step's types: {exc}"
        ) from exc
    max_turns = runtime.max_turns or DEFAULT_MAX_TURNS
    agent = Agent(
        name=request.step_name,
        instructions=_instructions(
            signature.instructions,
            runtime.system_prompt_append,
            request.skills,
            narrate=runtime.narrate,
        ),
        tools=tools,
        model=runtime.model,
        model_settings=ModelSettings(**runtime.model_settings),
        output_type=answer_schema,
    )
    # File outputs go to a fresh folder in the run workspace, as with every runtime.
    output_folder = AgentOutputFolder(request.step_name) if file_outputs else None
    trace_id = gen_trace_id() if runtime.tracing else None
    async with contextlib.AsyncExitStack() as stack:
        try:
            models = await stack.enter_async_context(_models())
        except OpenAIError as exc:
            raise AgentStepExecutionError(
                f"agent step {request.step_name!r} could not create its OpenAI client: {exc}"
            ) from exc
        run_config = RunConfig(
            model_provider=models,
            # The SDK sends its own traces to OpenAI only when the step asks for it.
            tracing_disabled=not runtime.tracing,
            trace_id=trace_id,
            workflow_name=f"avalanche: {request.step_name}",
        )
        model_input = _model_input(request, file_outputs=bool(file_outputs))
        # The trace records file inputs by reference, never by their bytes.
        inputs, omitted_inputs = retained_fields(
            {name: getattr(request.inputs, name) for name in signature.input_model.model_fields}
        )
        await events.emit(
            None, RunStarted(model=runtime.model, inputs=inputs, omitted_inputs=omitted_inputs)
        )
        transcript = _Transcript(
            events, tool_errors, model=runtime.model, max_turns=max_turns, trace_id=trace_id
        )
        started = time.monotonic()
        streamed = Runner.run_streamed(
            agent, input=model_input, max_turns=max_turns, run_config=run_config
        )
        try:
            async for event in streamed.stream_events():
                await transcript.add(event)
        except asyncio.CancelledError:
            streamed.cancel()
            events.unavailable(
                status="cancelled",
                error="the agent call was cancelled",
                external_run_id=trace_id,
                complete=True,
            )
            raise
        except Exception as exc:
            # The turn in progress keeps the tool calls it made before the run failed.
            await transcript.finish_turn()
            error = f"{type(exc).__name__}: {exc}"
            events.finished(
                transcript.header(
                    status="failed",
                    stop_reason=_snake_case(type(exc).__name__),
                    usage=streamed.context_wrapper.usage,
                    duration_ms=_elapsed_ms(started),
                    errors=(error[:_MAX_ERROR_CHARS],),
                ),
                outputs=None,
                complete=True,
            )
            raise AgentStepExecutionError(
                f"agent step {request.step_name!r} failed calling OpenAI: {error}"
            ) from exc
        answer = _ANSWER.validate_python(streamed.final_output)
        summary = answer.get(_SUMMARY_FIELD) if runtime.narrate else None
        # The answering turn says what the model did, in the summary it answered with.
        await transcript.finish_turn(
            answer=True, summary=summary if isinstance(summary, str) else None
        )
        record = AgentTrace(
            header=transcript.header(
                status="succeeded",
                stop_reason="final_output",
                usage=streamed.context_wrapper.usage,
                duration_ms=_elapsed_ms(started),
                errors=(),
            ),
            turns=tuple(transcript.turns),
        )
        try:
            if runtime.narrate:
                if not isinstance(summary, str):
                    raise AgentOutputError(
                        f"agent step {request.step_name!r}: the model's answer has no "
                        f"{_SUMMARY_FIELD!r} text"
                    )
                del answer[_SUMMARY_FIELD]
            outputs = _outputs(request, answer, file_outputs, output_folder)
        except Exception:
            # The step fails, and its final event says the run returned no outputs.
            events.finished(record.header, outputs=None, complete=True)
            raise
        events.finished(record.header, outputs=outputs, complete=True)
        return AgentRunOutcome(outputs=outputs, trace=record)


@contextlib.asynccontextmanager
async def _models() -> AsyncIterator[ModelProvider]:
    """The OpenAI models of one call, through a client of its own, closed with the call.

    The SDK's shared client stays bound to the event loop that first used it, and
    steps may each run on their own loop. The client reads ``OPENAI_API_KEY`` and
    the other ``OPENAI_*`` settings from the environment.
    """
    async with AsyncOpenAI() as client:
        yield OpenAIProvider(openai_client=client)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _snake_case(name: str) -> str:
    """``MaxTurnsExceeded`` as ``max_turns_exceeded``, ``APIError`` as ``api_error``."""
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", words).lower()


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


def _check_fields(request: AgentRequest) -> None:
    """Files only as whole ``ava.File`` or ``list[ava.File]`` fields; no workspaces."""
    signature = request.signature
    for role, model in (("input", signature.input_model), ("output", signature.output_model)):
        for name, info in model.model_fields.items():
            if _mentions(info.annotation, Workspace):
                raise AgentStepError(
                    f"agent step {request.step_name!r}: OpenAIAgent {role} {name!r} holds an "
                    "ava.Workspace, but the agent has no file system; pass files as ava.File "
                    "inputs, or run the step on ClaudeAgent"
                )
            if info.annotation not in (File, list[File]) and _mentions(info.annotation, File):
                raise AgentStepError(
                    f"agent step {request.step_name!r}: OpenAIAgent {role} {name!r} must be "
                    "typed exactly ava.File or list[ava.File] when it holds files"
                )


class _AnswerSchema(AgentOutputSchemaBase):
    """The step's answer: a JSON object in the signature's shape, checked by Avalanche."""

    def __init__(self, schema: dict[str, Any]) -> None:
        self._schema = ensure_strict_json_schema(schema)

    def is_plain_text(self) -> bool:
        return False

    def name(self) -> str:
        return "step_answer"

    def json_schema(self) -> dict[str, Any]:
        return self._schema

    def is_strict_json_schema(self) -> bool:
        return True

    def validate_json(self, json_str: str) -> dict[str, JsonValue]:
        try:
            return _ANSWER.validate_json(json_str)
        except ValidationError as exc:
            raise ModelBehaviorError(f"the model's answer is not a JSON object: {exc}") from exc


# The answer field holding the run summary that narration asks for.
_SUMMARY_FIELD = "run_summary"

_NARRATION = (
    "While you work, keep a short running account of what you are doing. Before each "
    "tool call, write one or two plain sentences saying what you are about to do and "
    "why. When a result changes your plan, say how. When you submit your answer, put a "
    f"brief summary of what you did and what you found in {_SUMMARY_FIELD}. Keep these "
    "notes short: they are a progress log for the people watching this run, not the "
    "answer."
)


def _output_schema(
    model: type[BaseModel], file_outputs: Mapping[str, bool], *, summary: bool
) -> dict[str, Any]:
    """The signature's output schema; a file output is the name and text of a file.

    With ``summary``, the schema also requires the run summary that becomes the
    answering turn's message; it is removed before the outputs are built.
    """
    schema = model.model_json_schema()
    if summary:
        schema["properties"][_SUMMARY_FIELD] = {
            "type": "string",
            "description": (
                "A few plain sentences for the people watching this run: what you did and "
                "what you found. Not part of the answer."
            ),
        }
        schema["required"] = [*schema.get("required", ()), _SUMMARY_FIELD]
    written = {
        "type": "object",
        "description": "A text file you write for this output.",
        "properties": {
            "name": {
                "type": "string",
                "description": "The file's name, such as report.md, without folders.",
            },
            "content": {"type": "string", "description": "The file's whole text."},
        },
        "required": ["name", "content"],
        "additionalProperties": False,
    }
    for name, single in file_outputs.items():
        schema["properties"][name] = written if single else {"type": "array", "items": written}
    return schema


def _instructions(
    instructions: str, append: str | None, skills: tuple[Skill, ...], *, narrate: bool
) -> str:
    parts = [instructions.strip()]
    if narrate:
        parts.append(_NARRATION)
    if skills:
        listing = "\n".join(f"- {skill.name}: {skill.description}" for skill in skills)
        parts.append(
            "You can use these Agent Skills. Each holds instructions for a kind of task. "
            f"When the task matches a skill's description, call {_LOAD_SKILL} with its name "
            "and follow its instructions; read a file the instructions refer to with "
            f"{_READ_SKILL_FILE}. You can read a skill's scripts but not run them.\n\n"
            + listing
        )
    if append:
        parts.append(append.strip())
    return "\n\n".join(parts)


def _model_input(request: AgentRequest, *, file_outputs: bool) -> list[ResponseInputItemParam]:
    """One user message: the instruction and the inputs as JSON, then each input file."""
    values: dict[str, JsonValue] = {}
    attachments: list[File] = []

    def attach(file: File) -> str:
        attachments.append(file)
        return f"attachment {len(attachments)}: {_file_name(file)}"

    for name, info in request.signature.input_model.model_fields.items():
        value = getattr(request.inputs, name)
        if info.annotation is File:
            values[name] = attach(value)
        elif info.annotation == list[File]:
            values[name] = [attach(item) for item in value]
        else:
            values[name] = to_jsonable_python(value)
    prompt = "Complete the task for these inputs."
    if attachments:
        prompt += " Files are attached after this message, numbered as the inputs name them."
    prompt += "\n\n" + json.dumps(values, indent=2, ensure_ascii=False)
    if file_outputs:
        prompt += "\n\nAnswer each file output with the file's name and its whole text."
    content: list[ResponseInputTextParam | ResponseInputImageParam | ResponseInputFileParam] = [
        ResponseInputTextParam(type="input_text", text=prompt)
    ]
    for number, file in enumerate(attachments, start=1):
        content.append(
            ResponseInputTextParam(
                type="input_text", text=f"Attachment {number}: {_file_name(file)}"
            )
        )
        content.append(_file_part(file))
    return [EasyInputMessageParam(role="user", content=content)]


def _file_name(file: File) -> str:
    return file.name or file.path.name


def _file_part(file: File) -> ResponseInputImageParam | ResponseInputFileParam:
    """A file as the Responses API takes it: an image, or a file the model reads."""
    name = _file_name(file)
    # An unknown type is sent as plain bytes; the API rejects a file it cannot read.
    content_type = (
        file.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    )
    data = f"data:{content_type};base64,{base64.b64encode(file.read_bytes()).decode()}"
    if content_type.startswith("image/"):
        return ResponseInputImageParam(type="input_image", image_url=data, detail="auto")
    return ResponseInputFileParam(type="input_file", filename=name, file_data=data)


class _WrittenFile(BaseModel):
    """A file output as the model answers it."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: str
    content: str


def _write(folder: AgentOutputFolder, written: _WrittenFile) -> File:
    name = written.name
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"{name!r} is not a file name without folders")
    path = folder.path / name
    if path.exists():
        raise ValueError(f"the answer names the file {name!r} more than once")
    path.write_text(written.content, encoding="utf-8")
    return folder.file(path)


def _outputs(
    request: AgentRequest,
    answer: dict[str, JsonValue],
    file_outputs: Mapping[str, bool],
    output_folder: AgentOutputFolder | None,
) -> AgentOutputs:
    """The model's answer, with the files it wrote as references in the run workspace."""
    if output_folder is None:
        return JsonOutputs(text=json.dumps(answer))
    signature = request.signature
    files: dict[str, File | list[File]] = {}
    for name, single in file_outputs.items():
        try:
            if single:
                files[name] = _write(output_folder, _WrittenFile.model_validate(answer[name]))
            else:
                files[name] = [
                    _write(output_folder, written)
                    for written in TypeAdapter(list[_WrittenFile]).validate_python(answer[name])
                ]
        except (OSError, ValueError) as exc:
            # ValidationError is a ValueError: a missing or malformed file.
            raise AgentOutputError(
                f"agent step {request.step_name!r} output {name!r} is not a usable file: {exc}"
            ) from exc
    try:
        validated = signature.validate_outputs(
            JsonOutputs(
                text=json.dumps(
                    {
                        **answer,
                        **{name: json.loads(to_json(value)) for name, value in files.items()},
                    }
                )
            )
        )
    except ValidationError as exc:
        raise AgentOutputError(
            f"agent step {request.step_name!r}: the model's answer does not match the "
            f"signature: {exc}"
        ) from exc
    values = {name: getattr(validated, name) for name in signature.output_model.model_fields}
    values.update(files)
    return PythonOutputs(values=values)


ToolErrors = dict[str, str]


def _error_recorder(errors: ToolErrors) -> Callable[[RunContextWrapper[None], Exception], str]:
    """Report a failed tool call to the model, which can recover, and keep it for the trace."""

    def record(context: RunContextWrapper[None], error: Exception) -> str:
        if not isinstance(context, ToolContext):
            raise AgentStepExecutionError(
                f"the OpenAI Agents SDK reported a tool error outside a tool call: {error}"
            )
        message = f"{type(error).__name__}: {error}"
        errors[context.tool_call_id] = message
        return message

    return record


def _step_tool(spec: ToolSpec, errors: ToolErrors) -> FunctionTool:
    """A step tool whose result reaches the model as JSON."""
    function = spec.function
    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def call(*args: object, **kwargs: object) -> str:
            return to_json(await function(*args, **kwargs)).decode()

    else:

        @functools.wraps(function)
        def call(*args: object, **kwargs: object) -> str:
            return to_json(function(*args, **kwargs)).decode()

    # The SDK reads the parameters through the wrapper; the result is always JSON text.
    call.__annotations__ = {
        name: hint for name, hint in function.__annotations__.items() if name != "return"
    }
    return function_tool(
        call,
        name_override=spec.name,
        description_override=spec.description,
        failure_error_function=_error_recorder(errors),
    )


def _skill_tools(skills: tuple[Skill, ...], errors: ToolErrors) -> list[FunctionTool]:
    """Tools that read the step's skills: SKILL.md first, then the files it refers to."""
    by_name = {skill.name: skill for skill in skills}

    def find(name: str) -> Skill:
        if name not in by_name:
            raise ValueError(f"there is no skill {name!r}; the skills are {sorted(by_name)}")
        return by_name[name]

    def load_skill(name: str) -> str:
        skill = find(name)
        text = skill.skill_md()
        if skill.files:
            listing = "\n".join(f"- {path}" for path in sorted(skill.files))
            text += f"\nFiles of this skill, read with {_READ_SKILL_FILE}:\n{listing}\n"
        return text

    def read_skill_file(skill: str, path: str) -> str:
        found = find(skill)
        if path not in found.files:
            raise ValueError(
                f"skill {skill!r} has no file {path!r}; its files are {sorted(found.files)}"
            )
        return found.files[path].read_text(encoding="utf-8")

    record = _error_recorder(errors)
    return [
        function_tool(
            load_skill,
            name_override=_LOAD_SKILL,
            description_override="Read an Agent Skill's SKILL.md: its instructions and the "
            "files they may refer to.",
            failure_error_function=record,
        ),
        function_tool(
            read_skill_file,
            name_override=_READ_SKILL_FILE,
            description_override="Read a text file of an Agent Skill, by its path inside "
            "the skill's folder.",
            failure_error_function=record,
        ),
    ]


class _FunctionCallOutput(BaseModel):
    """A tool result as the SDK sends it back to the model."""

    model_config = ConfigDict(extra="ignore", strict=True)

    call_id: str
    output: JsonValue


def _token_usage(
    *, input_tokens: int, output_tokens: int, cached: int, cache_written: int
) -> TokenUsage:
    """OpenAI counts cached and cache-written input in ``input_tokens``; Avalanche does not."""
    return TokenUsage(
        input_tokens=input_tokens - cached - cache_written,
        output_tokens=output_tokens,
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=cache_written,
    )


def _response_usage(usage: ResponseUsage) -> TokenUsage:
    return _token_usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached=usage.input_tokens_details.cached_tokens,
        cache_written=usage.input_tokens_details.cache_write_tokens,
    )


def _run_usage(usage: Usage) -> TokenUsage | None:
    """The whole run's usage; ``None`` when it made no model request."""
    if usage.requests == 0:
        return None
    return _token_usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached=usage.input_tokens_details.cached_tokens,
        cache_written=usage.input_tokens_details.cache_write_tokens,
    )


def _bounded(value: object) -> JsonValue:
    """A JSON value for the trace, replaced by a marker when it is too large."""
    projected = _JSON.validate_python(to_jsonable_python(value))
    if len(to_json(projected)) <= _MAX_VALUE_BYTES:
        return projected
    return {"kind": "unavailable", "reason": "value exceeds the trace size limit"}


def _arguments(text: str) -> JsonValue:
    """Tool arguments as JSON; the model's own text when it is not valid JSON."""
    try:
        return _bounded(_JSON.validate_json(text))
    except ValidationError:
        return _bounded(text)


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

    response: Response
    started: float
    calls: dict[str, ToolCall] = field(default_factory=dict)
    call_started: dict[str, float] = field(default_factory=dict)


class _Transcript:
    """Turns the SDK's stream events into live events and a trace record."""

    def __init__(
        self,
        events: AgentEventSink,
        tool_errors: ToolErrors,
        *,
        model: str | None,
        max_turns: int,
        trace_id: str | None,
    ) -> None:
        self._events = events
        self._tool_errors = tool_errors
        self._configured_model = model
        self._max_turns = max_turns
        self._trace_id = trace_id
        self._pending: _PendingTurn | None = None
        self._response_started = time.monotonic()
        self.turns: list[Turn] = []
        self.model: str | None = None
        self.last_response_id: str | None = None

    async def add(self, event: StreamEvent) -> None:
        match event:
            case AgentUpdatedStreamEvent(new_agent=agent):
                await self._events.emit(
                    native.AgentStarted(
                        agent_name=agent.name,
                        model=self._configured_model,
                        tools=tuple(tool.name for tool in agent.tools),
                    ),
                    None,
                )
            case RawResponsesStreamEvent(data=ResponseCreatedEvent()):
                # A new response means the previous one's tool calls are done.
                await self.finish_turn()
                self._response_started = time.monotonic()
            case RawResponsesStreamEvent(data=ResponseCompletedEvent(response=response)):
                self._pending = _PendingTurn(response, started=self._response_started)
                self.model = self.model or response.model
                self.last_response_id = response.id
            case RunItemStreamEvent(name="tool_called", item=ToolCallItem(raw_item=raw)):
                await self._tool_called(raw)
            case RunItemStreamEvent(name="tool_output", item=ToolCallOutputItem(raw_item=raw)):
                await self._tool_output(_FunctionCallOutput.model_validate(raw))
            # Text and argument deltas, and the message and reasoning items already in
            # the completed response, carry nothing more the trace keeps.

    def _pending_turn(self) -> _PendingTurn:
        if self._pending is None:
            raise AgentStepExecutionError(
                "the OpenAI Agents SDK reported a tool call outside a model response"
            )
        return self._pending

    async def _tool_called(self, raw: object) -> None:
        if not isinstance(raw, ResponseFunctionToolCall):
            raise AgentStepExecutionError(
                f"the model called a {type(raw).__name__}, a tool OpenAIAgent does not offer"
            )
        pending = self._pending_turn()
        pending.calls[raw.call_id] = ToolCall(
            call_id=raw.call_id,
            caller="model",
            name=raw.name,
            arguments=_arguments(raw.arguments),
            result=None,
            error=None,
            duration_ms=None,
        )
        pending.call_started[raw.call_id] = time.monotonic()
        await self._events.emit(
            None, ToolStarted(call_id=raw.call_id, name=raw.name, turn=len(self.turns) + 1)
        )

    async def _tool_output(self, output: _FunctionCallOutput) -> None:
        pending = self._pending_turn()
        if output.call_id not in pending.calls:
            raise AgentStepExecutionError(
                f"the OpenAI Agents SDK returned a result for unknown tool call "
                f"{output.call_id!r}"
            )
        call = pending.calls[output.call_id]
        error = self._tool_errors.pop(output.call_id, None)
        pending.calls[output.call_id] = call.model_copy(
            update={
                "result": None if error is not None else _bounded(output.output),
                "error": error,
                "duration_ms": _elapsed_ms(pending.call_started[output.call_id]),
            }
        )
        await self._events.emit(
            None,
            ToolFinished(
                call_id=output.call_id, name=call.name, turn=len(self.turns) + 1, error=error
            ),
        )

    async def finish_turn(self, *, answer: bool = False, summary: str | None = None) -> None:
        """Record the response in progress as a turn.

        The answering response's text is the JSON answer; with a run summary, the turn
        shows the summary instead, since the outputs already hold the answer.
        """
        pending, self._pending = self._pending, None
        if pending is None:
            return
        response = pending.response
        messages = [item for item in response.output if isinstance(item, ResponseOutputMessage)]
        written = "\n\n".join(
            part.text
            for message in messages
            for part in message.content
            if isinstance(part, ResponseOutputText)
        )
        refusal = "\n\n".join(
            part.refusal
            for message in messages
            for part in message.content
            if isinstance(part, ResponseOutputRefusal)
        )
        reasoning = "\n\n".join(
            text
            for item in response.output
            if isinstance(item, ResponseReasoningItem)
            for text in (
                *(summary_part.text for summary_part in item.summary),
                *(content.text for content in item.content or ()),
            )
        )
        text = summary if answer and summary is not None else written
        usage = response.usage
        turn = Turn(
            index=len(self.turns) + 1,
            message=text or None,
            reasoning=reasoning or None,
            tool_calls=tuple(pending.calls.values()),
            duration_ms=_elapsed_ms(pending.started),
            usage=None if usage is None else _response_usage(usage),
            native=native.OpenAIAgentTurn(
                response_id=response.id,
                model=response.model,
                reasoning_tokens=None
                if usage is None
                else usage.output_tokens_details.reasoning_tokens,
                refusal=refusal or None,
                answer=answer,
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

    def header(
        self,
        *,
        status: AgentTraceStatus,
        stop_reason: str,
        usage: Usage,
        duration_ms: int,
        errors: tuple[str, ...],
    ) -> AgentTraceHeader:
        return AgentTraceHeader(
            status=status,
            stop_reason=stop_reason,
            model=self.model or self._configured_model,
            duration_ms=duration_ms,
            usage=_run_usage(usage),
            turn_count=len(self.turns),
            tool_call_count=sum(len(turn.tool_calls) for turn in self.turns),
            external_run_id=self._trace_id,
            session_id=None,
            native=native.OpenAIAgentTraceHeader(
                max_turns=self._max_turns,
                requests=usage.requests,
                reasoning_tokens=usage.output_tokens_details.reasoning_tokens
                if usage.requests
                else None,
                last_response_id=self.last_response_id,
                errors=errors,
            ),
        )
