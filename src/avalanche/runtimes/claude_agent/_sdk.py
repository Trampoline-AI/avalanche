"""Run one agent call through the Claude Agent SDK; the only module importing the SDK."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import json
import shutil
import tempfile
import types
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLINotFoundError,
    HookCallback,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    Message,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
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
    AgentOutputs,
    AgentRequest,
    AgentRunOutcome,
    JsonOutputs,
    PythonOutputs,
    ToolSpec,
)
from ...agent.live import RunStarted, ToolFinished, ToolStarted, TurnFinished
from ...agent.native import claude_agent as native
from ...agent.skills import SKILL_FILE, Skill
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
from .runtime import unknown_sdk_options

if TYPE_CHECKING:
    from .runtime import ClaudeAgent

_SERVER = "avalanche"
# The plugin that carries a step's Agent Skills; Claude names each skill
# ``avalanche:<name>``.
_SKILL_PLUGIN = "avalanche"
_JSON = TypeAdapter(JsonValue)
# Tool arguments and results kept in a trace; larger values are replaced by a marker.
_MAX_VALUE_BYTES = 256 * 1024
# A turn's tool calls kept in its live event; later calls are counted as omitted, so the
# event stays well under the operator's 8 MiB agent-event limit.
_MAX_TURN_TOOL_CALL_BYTES = 4 * 1024 * 1024
# An error reason taken from a result's free text is cut to this length.
_MAX_ERROR_CHARS = 4096
# Listed result errors kept in the trace header, which every run snapshot carries:
# even fully JSON-escaped (6 bytes a character) they stay under its 64 KiB limit.
_MAX_RESULT_ERRORS = 8
_MAX_RESULT_ERROR_CHARS = 1024


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
    file_outputs = {
        name: info.annotation is File
        for name, info in signature.output_model.model_fields.items()
        if info.annotation in (File, list[File])
    }
    unsupported_outputs = [
        name
        for name, info in signature.output_model.model_fields.items()
        if name not in file_outputs
        and (
            _mentions(info.annotation, File)
            or (info.annotation is not Workspace and _mentions(info.annotation, Workspace))
        )
    ]
    if unsupported_outputs:
        raise AgentStepError(
            f"agent step {request.step_name!r}: ClaudeAgent outputs that hold files must be "
            f"typed exactly ava.File, list[ava.File] or ava.Workspace {unsupported_outputs}"
        )
    if runtime.narrate and _SUMMARY_FIELD in signature.output_model.model_fields:
        raise AgentStepError(
            f"agent step {request.step_name!r}: with narrate on, ClaudeAgent asks Claude for "
            f"its own {_SUMMARY_FIELD!r} answer field; rename the signature's output or set "
            "narrate=False"
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
        # File outputs go to a fresh folder in the run workspace, as with every runtime.
        output_folder = AgentOutputFolder(request.step_name) if file_outputs else None
        # The step's skills go in a plugin folder made for this call, never in the
        # user's Claude configuration or a workspace the step returns.
        plugin = (
            _write_skill_plugin(
                Path(
                    stack.enter_context(
                        tempfile.TemporaryDirectory(prefix="avalanche-claude-skills-")
                    )
                ),
                request.skills,
            )
            if request.skills
            else None
        )
        prompt_inputs = _prompt_inputs(request, directory)
        # Claude reads input files where they are, without copies; folders outside the
        # working directory are added so the CLI may reach them.
        add_dirs = sorted(
            {
                str(path)
                for path in (
                    *(file.path.parent for file in _input_files(request)),
                    *((output_folder.path,) if output_folder is not None else ()),
                    *((plugin,) if plugin is not None else ()),
                )
                if not path.is_relative_to(directory)
            }
        )
        builtin_tools = list(runtime.builtin_tools)
        skill_tools: list[str] = []
        if plugin is not None:
            skill_tools.append("Skill")
            # A skill's resource files are read with Read. Added for skills only, it
            # reads nothing outside the plugin, whatever the permission mode.
            if any(skill.files for skill in request.skills) and "Read" not in builtin_tools:
                skill_tools.append("Read")
        # A Read the user enables through the SDK ``tools`` option keeps its usual
        # scope, as one in builtin_tools does.
        read_guard = "Read" in skill_tools and not _lists_read(runtime.sdk_options)
        options = ClaudeAgentOptions(
            model=runtime.model,
            max_turns=runtime.max_turns,
            max_budget_usd=runtime.max_budget_usd,
            permission_mode=runtime.permission_mode,
            # User and project Claude settings never leak into a workflow step.
            setting_sources=[],
            tools=[*builtin_tools, *skill_tools],
            allowed_tools=[
                *builtin_tools,
                *(f"mcp__{_SERVER}__{spec.name}" for spec in request.tools),
                *((f"Read(/{plugin}/**)",) if "Read" in skill_tools else ()),
            ],
            # Only the step's skills: the CLI's bundled skills stay hidden. With
            # ``skills`` set, the SDK allows the Skill tool for each listed name.
            plugins=[{"type": "local", "path": str(plugin)}] if plugin is not None else [],
            skills=(
                [f"{_SKILL_PLUGIN}:{skill.name}" for skill in request.skills]
                if request.skills
                else None
            ),
            mcp_servers={_SERVER: _tool_server(request.tools)} if request.tools else {},
            # Only the step's own tools: no MCP servers or connectors from the user's
            # Claude account or configuration.
            strict_mcp_config=True,
            hooks=(
                {"PreToolUse": [HookMatcher(matcher="Read", hooks=[_read_only_inside(plugin)])]}
                if read_guard
                else None
            ),
            system_prompt=_system_prompt(
                signature.instructions, runtime.system_prompt_append, narrate=runtime.narrate
            ),
            output_format={
                "type": "json_schema",
                "schema": _output_schema(
                    signature.output_model,
                    workspace_outputs,
                    file_outputs,
                    summary=runtime.narrate,
                ),
            },
            cwd=str(directory),
            add_dirs=add_dirs,
            # Each response's final usage and stop reason arrive only as a stream event.
            include_partial_messages=True,
        )
        options = _with_sdk_options(options, runtime.sdk_options, step_name=request.step_name)
        transcript = _Transcript(events, summary=runtime.narrate)
        # The trace records file inputs by reference, never by host path.
        inputs, omitted_inputs = retained_fields(
            {name: getattr(request.inputs, name) for name in signature.input_model.model_fields}
        )
        await events.emit(
            None,
            RunStarted(model=runtime.model, inputs=inputs, omitted_inputs=omitted_inputs),
        )
        try:
            # Closing the stream waits for the CLI to stop, even when handling a message
            # raises, so the working folder outlives it.
            async with contextlib.aclosing(
                _session(
                    prompt=_prompt(prompt_inputs, output_folder, directory), options=options
                )
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
        if record.header.status != "succeeded":
            events.finished(record.header, outputs=None, complete=True)
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
        try:
            outputs = _outputs(
                request,
                _answer(request, result.structured_output, summary=runtime.narrate),
                workspace=workspace,
                directory=directory,
                workspace_outputs=workspace_outputs,
                file_outputs=file_outputs,
                output_folder=output_folder,
            )
        except Exception:
            # The step fails, and its final event says the run returned no outputs.
            events.finished(record.header, outputs=None, complete=True)
            raise
        events.finished(record.header, outputs=outputs, complete=True)
        return AgentRunOutcome(outputs=outputs, trace=record)


def _answer(
    request: AgentRequest, structured_output: dict[str, Any], *, summary: bool
) -> dict[str, Any]:
    """Claude's structured output without the run summary Avalanche asked for."""
    if not summary:
        return structured_output
    if _summary_of(structured_output) is None:
        raise AgentOutputError(
            f"agent step {request.step_name!r}: Claude's answer has no {_SUMMARY_FIELD!r} text"
        )
    return {name: value for name, value in structured_output.items() if name != _SUMMARY_FIELD}


def _summary_of(structured_output: object) -> str | None:
    """The run summary in Claude's answer; a failed session may have no answer."""
    if not isinstance(structured_output, dict):
        return None
    summary = structured_output.get(_SUMMARY_FIELD)
    return summary if isinstance(summary, str) else None


def _outputs(
    request: AgentRequest,
    structured_output: dict[str, Any],
    *,
    workspace: Workspace | None,
    directory: Path,
    workspace_outputs: list[str],
    file_outputs: Mapping[str, bool],
    output_folder: AgentOutputFolder | None,
) -> AgentOutputs:
    """Claude's structured output, with the workspaces and files Avalanche returns."""
    signature = request.signature
    if not workspace_outputs and output_folder is None:
        return JsonOutputs(text=json.dumps(structured_output))
    # A reference snapshot is bound to its directory, which a JSON round trip loses,
    # so Claude's values are validated here and the snapshot joins them as an object.
    snapshot = (
        (workspace.snapshot() if workspace is not None else Workspace.from_path(directory))
        if workspace_outputs
        else None
    )
    outputs: dict[str, object] = dict(structured_output)
    files = (
        _output_files(request, outputs, file_outputs, output_folder, directory)
        if output_folder is not None
        else {}
    )
    for name in workspace_outputs:
        outputs[name] = json.loads(snapshot.model_dump_json())
    for name in file_outputs:
        outputs[name] = json.loads(to_json(files[name]))
    try:
        validated = signature.validate_outputs(JsonOutputs(text=json.dumps(outputs)))
    except ValidationError as exc:
        raise AgentOutputError(
            f"agent step {request.step_name!r}: Claude's structured output does not match "
            f"the signature: {exc}"
        ) from exc
    values = {name: getattr(validated, name) for name in signature.output_model.model_fields}
    values.update(dict.fromkeys(workspace_outputs, snapshot))
    values.update(files)
    return PythonOutputs(values=values)


# SDK options Avalanche fills that are combined with the user's, by the shape they need.
_COMBINED_OPTIONS: Mapping[str, type] = types.MappingProxyType(
    {
        "mcp_servers": dict,
        "hooks": dict,
        "plugins": list,
        "allowed_tools": list,
        "add_dirs": list,
    }
)


def _with_sdk_options(
    options: ClaudeAgentOptions, given: Mapping[str, Any], *, step_name: str
) -> ClaudeAgentOptions:
    """Avalanche's options for the step with the SDK options the user gave.

    Collections Avalanche fills are combined with the user's entries; any other
    option the user gives replaces Avalanche's value.
    """
    if problem := unknown_sdk_options(
        given.keys(), {option.name for option in dataclasses.fields(ClaudeAgentOptions)}
    ):
        raise AgentStepError(f"agent step {step_name!r}: {problem}")
    for name, shape in _COMBINED_OPTIONS.items():
        if name in given and not isinstance(given[name], shape):
            raise AgentStepError(
                f"agent step {step_name!r}: ClaudeAgent {name} must be a {shape.__name__}, "
                "which Avalanche combines with its own entries for the step"
            )
    changes = dict(given)
    if "mcp_servers" in given:
        if _SERVER in given["mcp_servers"]:
            raise AgentStepError(
                f"agent step {step_name!r}: the MCP server name {_SERVER!r} is reserved for "
                "the step's tools; give your server another name"
            )
        assert isinstance(options.mcp_servers, dict)
        changes["mcp_servers"] = {**options.mcp_servers, **given["mcp_servers"]}
    if "hooks" in given:
        hooks = {event: list(matchers) for event, matchers in (options.hooks or {}).items()}
        for event, matchers in given["hooks"].items():
            hooks.setdefault(event, []).extend(matchers)
        changes["hooks"] = hooks
    if "plugins" in given:
        changes["plugins"] = [*options.plugins, *given["plugins"]]
    for name in ("allowed_tools", "add_dirs"):
        if name in given:
            changes[name] = _union(getattr(options, name), given[name])
    # A tool preset or ``"all"`` skills already include the step's tools and skills.
    if isinstance(given.get("tools"), list):
        assert isinstance(options.tools, list)
        changes["tools"] = _union(options.tools, given["tools"])
    if isinstance(given.get("skills"), list):
        assert options.skills != "all"
        changes["skills"] = _union(options.skills or [], given["skills"])
    return dataclasses.replace(options, **changes)


def _lists_read(given: Mapping[str, Any]) -> bool:
    """Whether the user's SDK ``tools`` enable the whole Read tool.

    A preset or ``None`` enables every Claude Code tool, Read included. Listing Read in
    ``allowed_tools`` only approves it in advance, so it does not count.
    """
    if "tools" not in given:
        return False
    tools = given["tools"]
    return not isinstance(tools, list) or "Read" in tools


def _union[T](first: list[T], second: list[T]) -> list[T]:
    """``first`` then the entries of ``second`` it lacks, in order."""
    return [*first, *(entry for entry in second if entry not in first)]


def _write_skill_plugin(root: Path, skills: tuple[Skill, ...]) -> Path:
    """Write a Claude Code plugin holding ``skills`` under ``root``; return its folder.

    The plugin declares no MCP servers, hooks or agents: only skills.
    """
    plugin = root / _SKILL_PLUGIN
    manifest = plugin / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": _SKILL_PLUGIN,
                "description": "Agent Skills of one Avalanche agent step",
            }
        ),
        encoding="utf-8",
    )
    for skill in skills:
        folder = plugin / "skills" / skill.name
        folder.mkdir(parents=True)
        (folder / SKILL_FILE).write_text(skill.skill_md(), encoding="utf-8")
        for relative, source in skill.files.items():
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            # Keep permission bits, so a skill's scripts stay executable.
            shutil.copy(source, target)
    return plugin


class _ReadInput(BaseModel):
    """The part of a Read tool call that names the file; other options pass through."""

    model_config = ConfigDict(strict=True, extra="allow")

    file_path: str


def _read_only_inside(folder: Path) -> HookCallback:
    """A PreToolUse hook denying every Read outside ``folder``.

    Hooks run before permission checks, so the limit holds in every permission
    mode, ``bypassPermissions`` included, and for the session's working folders.
    """
    root = folder.resolve()

    async def check(
        hook_input: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        try:
            requested = _ReadInput.model_validate(hook_input["tool_input"]).file_path
        except ValidationError:
            requested = None
        if requested is not None and (
            (Path(hook_input["cwd"]) / requested).resolve().is_relative_to(root)
        ):
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Read is available only for skill files inside {root}"
                ),
            }
        }

    return check


def _input_files(request: AgentRequest) -> list[File]:
    files: list[File] = []
    for name, info in request.signature.input_model.model_fields.items():
        value = getattr(request.inputs, name)
        if info.annotation is File:
            files.append(value)
        elif info.annotation == list[File]:
            files.extend(value)
    return files


async def _session(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
    """The session's messages up to its result; closing it awaits the CLI's exit."""
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async with contextlib.aclosing(client.receive_response()) as messages:
            async for message in messages:
                yield message


def _prompt_path(path: Path, directory: Path) -> str:
    """A path relative to the working directory when it is inside it, else absolute."""
    return (
        path.relative_to(directory).as_posix() if path.is_relative_to(directory) else str(path)
    )


def _prompt_inputs(request: AgentRequest, directory: Path) -> dict[str, JsonValue]:
    """The inputs as JSON; files are named by their bound host path."""
    values: dict[str, JsonValue] = {}
    for name, info in request.signature.input_model.model_fields.items():
        value = getattr(request.inputs, name)
        if info.annotation is Workspace:
            values[name] = "the current working directory"
        elif info.annotation is File:
            values[name] = _prompt_path(value.path, directory)
        elif info.annotation == list[File]:
            values[name] = [_prompt_path(item.path, directory) for item in value]
        else:
            values[name] = to_jsonable_python(value)
    return values


def _prompt(
    inputs: Mapping[str, JsonValue], output_folder: AgentOutputFolder | None, directory: Path
) -> str:
    prompt = (
        "Complete the task for these inputs. Files are given as paths; relative paths are "
        "relative to the current working directory.\n\n"
        + json.dumps(inputs, indent=2, ensure_ascii=False)
    )
    if output_folder is not None:
        prompt += (
            "\n\nWrite every file output inside "
            f"{_prompt_path(output_folder.path, directory)} and give its path."
        )
    return prompt


def _output_files(
    request: AgentRequest,
    outputs: Mapping[str, object],
    file_outputs: Mapping[str, bool],
    output_folder: AgentOutputFolder,
    directory: Path,
) -> dict[str, File | list[File]]:
    """Claude's file output paths as references in the run workspace."""
    files: dict[str, File | list[File]] = {}
    for name, single in file_outputs.items():
        try:
            paths = TypeAdapter(str if single else list[str]).validate_python(outputs.get(name))
            if isinstance(paths, str):
                files[name] = output_folder.file(directory / paths)
            else:
                files[name] = [output_folder.file(directory / path) for path in paths]
        except (OSError, ValueError) as exc:
            # ValidationError is a ValueError: a missing or non-path value.
            raise AgentOutputError(
                f"agent step {request.step_name!r} output {name!r} is not a usable file: {exc}"
            ) from exc
    return files


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


def _system_prompt(instructions: str, append: str | None, *, narrate: bool) -> str:
    parts = [instructions.strip()]
    if narrate:
        parts.append(_NARRATION)
    if append:
        parts.append(append.strip())
    return "\n\n".join(parts)


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
        return tuple(
            error[:_MAX_RESULT_ERROR_CHARS] for error in result.errors[:_MAX_RESULT_ERRORS]
        )
    if result.is_error and result.result:
        return (result.result[:_MAX_ERROR_CHARS],)
    return ()


def _output_schema(
    model: type[BaseModel],
    workspace_outputs: list[str],
    file_outputs: Mapping[str, bool],
    *,
    summary: bool,
) -> dict[str, Any]:
    """The signature's output schema; Avalanche fills workspaces, files are paths.

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
    for name in workspace_outputs:
        del schema["properties"][name]
        schema["required"] = [field for field in schema.get("required", ()) if field != name]
    path = {"type": "string", "description": "Path of a file you wrote for this output."}
    for name, single in file_outputs.items():
        schema["properties"][name] = path if single else {"type": "array", "items": path}
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


class _StopDelta(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    stop_reason: str | None


class _MessageDelta(BaseModel):
    """A response's closing stream event: its stop reason and final token counts."""

    model_config = ConfigDict(extra="ignore", strict=True)

    type: Literal["message_delta"]
    delta: _StopDelta
    usage: _SdkUsage


class _SystemInit(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    session_id: str | None = None
    model: str | None = None
    tools: list[str] = []


def _usage(raw: Mapping[str, Any] | None, cost: float | None = None) -> TokenUsage | None:
    if raw is None:
        return None
    return _token_usage(_SdkUsage.model_validate(raw), cost)


def _token_usage(usage: _SdkUsage, cost: float | None = None) -> TokenUsage:
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
    calls: dict[str, ToolCall] = field(default_factory=dict)
    # From the response's closing stream event; the messages carry only the counts
    # from when the response started. API errors the CLI reports have no such event.
    final: _MessageDelta | None = None


class _Transcript:
    """Turns the SDK's message stream into live events and a trace record."""

    def __init__(self, events: AgentEventSink, *, summary: bool) -> None:
        self._events = events
        self._summary = summary
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
                | StreamEvent(parent_tool_use_id=str())
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
                        pending.calls[block.id] = ToolCall(
                            call_id=block.id,
                            caller="model",
                            name=block.name,
                            arguments=_bounded(block.input),
                            result=None,
                            error=None,
                            duration_ms=None,
                        )
                        await self._events.emit(
                            None,
                            ToolStarted(
                                call_id=block.id, name=block.name, turn=len(self.turns) + 1
                            ),
                        )
            case StreamEvent(event={"type": "message_delta"} as event):
                if self._pending is None:
                    raise AgentStepExecutionError(
                        "Claude closed a response before sending any of its content"
                    )
                self._pending.final = _MessageDelta.model_validate(event)
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
                # The answering turn says what Claude did, in the summary it answered with.
                await self._finish_turn(
                    summary=_summary_of(message.structured_output) if self._summary else None
                )
                # How the session ended travels in the final event's header.
                self.result = message
                self.session_id = message.session_id
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
            None,
            ToolFinished(
                call_id=call.call_id, name=call.name, turn=len(self.turns) + 1, error=error
            ),
        )

    async def _finish_turn(self, *, summary: str | None = None) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        message = pending.message
        written = "".join(
            block.text for block in pending.blocks if isinstance(block, TextBlock)
        )
        text = "\n\n".join(part for part in (written, summary) if part)
        reasoning = "".join(
            block.thinking for block in pending.blocks if isinstance(block, ThinkingBlock)
        )
        turn = Turn(
            index=len(self.turns) + 1,
            message=text or None,
            reasoning=reasoning or None,
            tool_calls=tuple(pending.calls.values()),
            duration_ms=None,
            usage=None if pending.final is None else _token_usage(pending.final.usage),
            native=native.ClaudeAgentTurn(
                message_id=message.message_id,
                model=message.model,
                stop_reason=message.stop_reason
                if pending.final is None
                else pending.final.delta.stop_reason,
                api_error=message.error,
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

    def record(self, result: ResultMessage, *, model: str | None) -> AgentTrace:
        succeeded = result.subtype == "success" and not result.is_error
        # Outputs are an object of fields; anything else fails the step, so the trace too.
        has_output = isinstance(result.structured_output, dict)
        status: AgentTraceStatus = "succeeded" if succeeded and has_output else "failed"
        return AgentTrace(
            header=AgentTraceHeader(
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
                tool_call_count=sum(len(turn.tool_calls) for turn in self.turns),
                external_run_id=result.session_id,
                session_id=result.session_id,
                native=native.ClaudeAgentTraceHeader(
                    result_subtype=result.subtype,
                    num_turns=result.num_turns,
                    duration_api_ms=result.duration_api_ms,
                    permission_denials=len(result.permission_denials or ()),
                    errors=_result_errors(result),
                ),
            ),
            turns=tuple(self.turns),
        )
