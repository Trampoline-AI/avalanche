"""The Claude Agent SDK runtime, replaying recorded SDK message streams."""

from __future__ import annotations

import asyncio
import importlib
import itertools
import os
import sys
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import BaseModel, Field

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    CLINotFoundError,
    ResultError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import avalanche as ava  # noqa: E402
from avalanche._agent_evidence import capture_agent_evidence  # noqa: E402
from avalanche.agent import (  # noqa: E402
    AgentOutputError,
    AgentStepError,
    AgentStepExecutionError,
)
from avalanche.agent import trace as trace_module  # noqa: E402
from avalanche.agent.contract import ToolSpec  # noqa: E402
from avalanche.agent.declaration import ClaudeAgentDeclaration  # noqa: E402
from avalanche.agent.live import RunStarted, TurnFinished  # noqa: E402
from avalanche.agent.native.claude_agent import ClaudeAgentTurn  # noqa: E402
from avalanche.agent.trace import TokenUsage, ToolCall  # noqa: E402
from avalanche.runtimes import ClaudeAgent  # noqa: E402

sdk_module = importlib.import_module("avalanche.runtimes.claude_agent._sdk")

MODEL = "claude-haiku-5-5"


class Total(ava.Signature):
    """Add the numbers and the bonus from lookup_bonus."""

    a: int = ava.InputField()
    b: int = ava.InputField()
    total: int = ava.OutputField()


def lookup_bonus(name: str) -> int:
    """Return the bonus for a name."""
    return 10


def _init() -> SystemMessage:
    return SystemMessage(
        subtype="init",
        data={
            "session_id": "session-1",
            "model": MODEL,
            "tools": ["mcp__avalanche__lookup_bonus"],
        },
    )


_MESSAGE_IDS = itertools.count(1)


def _assistant(*blocks, usage=None, message_id=None) -> AssistantMessage:
    """One assistant message; each gets its own ID unless it continues a response."""
    return AssistantMessage(
        content=list(blocks),
        model=MODEL,
        usage=usage or {"input_tokens": 3, "output_tokens": 5},
        message_id=message_id or f"msg-{next(_MESSAGE_IDS)}",
    )


def _closed(stop_reason: str, *, output_tokens: int) -> StreamEvent:
    """A response's closing stream event, with its final token counts."""
    return StreamEvent(
        uuid=f"event-{next(_MESSAGE_IDS)}",
        session_id="session-1",
        event={
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {
                "input_tokens": 3,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 40,
            },
        },
    )


def _result(
    structured_output, *, subtype="success", is_error=False, errors=None, result=None
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1200,
        duration_api_ms=900,
        is_error=is_error,
        num_turns=3,
        session_id="session-1",
        total_cost_usd=0.0004,
        usage={"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 4},
        structured_output=structured_output,
        errors=errors,
        result=result,
    )


def _success_stream(*, tool_error: bool = False, total: object = 15) -> list[object]:
    return [
        _init(),
        _assistant(
            ThinkingBlock(thinking="I need the bonus.", signature="sig"),
            TextBlock(text="Looking up the bonus."),
            ToolUseBlock(id="call-1", name="mcp__avalanche__lookup_bonus", input={"name": "a"}),
        ),
        _closed("tool_use", output_tokens=40),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="call-1",
                    content="lookup failed" if tool_error else "10",
                    is_error=tool_error,
                )
            ]
        ),
        _assistant(ToolUseBlock(id="call-2", name="StructuredOutput", input={"total": total})),
        _closed("tool_use", output_tokens=12),
        UserMessage(content=[ToolResultBlock(tool_use_id="call-2", content="ok")]),
        _result({"total": total}),
    ]


@pytest.fixture
def replay(monkeypatch):
    """Replace the Claude session with a recorded stream; returns the options it saw."""
    seen: dict[str, object] = {}

    def install(messages, *, error: BaseException | None = None):
        async def query(*, prompt, options):
            seen.update(prompt=prompt, options=options)
            for message in messages:
                yield message
            if error is not None:
                raise error

        monkeypatch.setattr(sdk_module, "_session", query)
        return seen

    return install


def _is_turn(event) -> bool:
    return event.kind == "event" and isinstance(event.common, TurnFinished)


def _agent(**runtime) -> ava.Agent:
    return ava.Agent(
        signature=Total,
        step_name="total",
        runtime=ClaudeAgent(model=MODEL, **runtime),
        tools=(lookup_bonus,),
    )


@pytest.mark.asyncio
async def test_a_session_maps_onto_outputs_turns_and_live_events(replay):
    seen = replay(_success_stream())
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await _agent(max_turns=6)(a=2, b=3)

    assert result.outputs.total == 15
    header = result.trace.header
    assert (header.runtime_kind, header.status, header.model) == (
        "claude_agent",
        "succeeded",
        MODEL,
    )
    assert (header.turn_count, header.tool_call_count) == (2, 2)
    assert header.external_run_id == header.session_id == "session-1"
    assert header.usage.cost_usd == pytest.approx(0.0004)
    first, second = result.trace.turns
    assert (first.reasoning, first.message) == ("I need the bonus.", "Looking up the bonus.")
    # Usage and stop reason come from each response's closing event, not the
    # counts its messages carry from when it started.
    assert first.native == ClaudeAgentTurn(
        message_id=first.native.message_id, model=MODEL, stop_reason="tool_use", api_error=None
    )
    assert [turn.usage for turn in result.trace.turns] == [
        TokenUsage(
            input_tokens=3,
            output_tokens=output_tokens,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=40,
        )
        for output_tokens in (40, 12)
    ]
    (call,) = first.tool_calls
    assert call == ToolCall(
        call_id="call-1",
        caller="model",
        name="mcp__avalanche__lookup_bonus",
        arguments={"name": "a"},
        result="10",
        error=None,
        duration_ms=None,
    )
    assert [call.name for call in second.tool_calls] == ["StructuredOutput"]
    # Only the session report is Claude's own; everything else is a common event.
    assert [
        (
            event.kind,
            event.native.kind if event.kind == "event" and event.native else None,
            event.common.kind if event.kind == "event" and event.common else None,
        )
        for event in observed
    ] == [
        ("event", None, "run.started"),
        ("event", "session.initialized", None),
        ("event", None, "tool.started"),
        ("event", None, "tool.finished"),
        ("event", None, "turn.finished"),
        ("event", None, "tool.started"),
        ("event", None, "tool.finished"),
        ("event", None, "turn.finished"),
        ("trace_finished", None, None),
    ]
    assert [event.turn for event in observed[2:8]] == [1, 1, 1, 2, 2, 2]
    assert observed[0].common.inputs == {"a": 2, "b": 3}
    assert observed[0].common.model == MODEL
    assert observed[-1].outputs == {"total": 15}

    options = seen["options"]
    assert options.model == MODEL
    assert options.max_turns == 6
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.tools == []
    assert options.allowed_tools == ["mcp__avalanche__lookup_bonus"]
    assert options.output_format["schema"]["properties"] == {
        "total": {"title": "Total", "type": "integer"}
    }
    assert options.include_partial_messages is True
    assert options.system_prompt.startswith("Add the numbers")
    assert options.system_prompt != Total.instructions.strip()
    assert '"a": 2' in seen["prompt"]


@pytest.mark.asyncio
async def test_narration_sits_between_the_instructions_and_the_append_and_can_be_off(replay):
    seen = replay(_success_stream())
    await _agent(system_prompt_append="Be terse.")(a=2, b=3)
    narrated = seen["options"].system_prompt
    replay(_success_stream())
    await _agent(narrate=False, system_prompt_append="Be terse.")(a=2, b=3)

    instructions = Total.instructions.strip()
    assert seen["options"].system_prompt == f"{instructions}\n\nBe terse."
    assert narrated.startswith(f"{instructions}\n\n") and narrated.endswith("\n\nBe terse.")
    assert len(narrated) > len(seen["options"].system_prompt)


@pytest.mark.asyncio
async def test_a_response_without_a_closing_event_reports_no_usage(replay):
    # API errors the CLI reports itself arrive as a message with no stream events.
    replay(
        [
            _init(),
            _assistant(
                ToolUseBlock(id="call-1", name="StructuredOutput", input={"total": 5}),
                usage={"input_tokens": 3, "output_tokens": 1},
            ),
            UserMessage(content=[ToolResultBlock(tool_use_id="call-1", content="ok")]),
            _result({"total": 5}),
        ]
    )
    result = await _agent()(a=2, b=3)

    (turn,) = result.trace.turns
    assert (turn.usage, turn.native.stop_reason) == (None, None)


class Note(ava.Signature):
    text: str = ava.InputField()
    answer: str = ava.OutputField()


@pytest.mark.asyncio
async def test_large_inputs_and_tool_calls_stay_within_the_live_event_limits(
    replay, monkeypatch
):
    monkeypatch.setattr(sdk_module, "_MAX_VALUE_BYTES", 64)
    monkeypatch.setattr(sdk_module, "_MAX_TURN_TOOL_CALL_BYTES", 1)
    monkeypatch.setattr(trace_module, "RETAINED_VALUES_LIMIT_BYTES", 64)
    seen = replay(_success_stream())
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await _agent()(a=2, b=3)
    turns = [e.common for e in observed if _is_turn(e)]
    # Over the budget, a live turn's calls are counted as omitted; the record keeps them.
    assert [(len(t.turn.tool_calls), t.omitted_tool_calls) for t in turns] == [(0, 1), (0, 1)]
    assert [len(turn.tool_calls) for turn in result.trace.turns] == [1, 1]

    seen = replay([_init(), _result({"answer": "ok"})])
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        await ava.Agent(signature=Note, step_name="note", runtime=ClaudeAgent())(text="x" * 500)
    # Claude gets the whole input; the run.started event names it as left out.
    assert "x" * 500 in seen["prompt"]
    assert (observed[0].common.inputs, observed[0].common.omitted_inputs) == ({}, ("text",))


@pytest.mark.asyncio
async def test_a_failed_tool_is_kept_on_its_call_and_does_not_fail_the_turn(replay):
    replay(_success_stream(tool_error=True))
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await _agent()(a=2, b=3)

    (call,) = result.trace.turns[0].tool_calls
    assert (call.result, call.error) == (None, "lookup failed")
    turn_event = next(e for e in observed if _is_turn(e))
    assert turn_event.failed is False


@pytest.mark.asyncio
async def test_a_turn_with_an_api_error_fails(replay):
    # The structured-output call's result is followed by a reply the API failed.
    stream = _success_stream()
    stream[-1:] = [
        AssistantMessage(
            content=[TextBlock(text="API Error: 429")],
            model=MODEL,
            error="rate_limit",
            usage={"input_tokens": 1, "output_tokens": 1},
            message_id="msg-api-error",
        ),
        _result({"total": 15}),
    ]
    replay(stream)
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await _agent()(a=2, b=3)

    assert result.trace.turns[-1].native.api_error == "rate_limit"
    turn_events = [e for e in observed if _is_turn(e)]
    assert [event.failed for event in turn_events] == [False, False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("structured_output", "stop_reason"),
    [(None, "no_structured_output"), ([15], "structured_output_not_object")],
    ids=["missing", "not-an-object"],
)
async def test_a_session_without_an_output_object_fails_after_its_trace(
    replay, structured_output, stop_reason
):
    replay([_init(), _assistant(TextBlock(text="15")), _result(structured_output)])
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AgentStepExecutionError, match="structured output"):
            await _agent()(a=2, b=3)

    final = observed[-1]
    assert final.kind == "trace_finished"
    assert (final.header.status, final.header.stop_reason) == (
        "failed",
        stop_reason,
    )


@pytest.mark.asyncio
async def test_running_out_of_turns_fails_with_the_sdk_reason(replay):
    # Like the real SDK, the stream raises ResultError after an error result.
    replay(
        [_init(), _result(None, subtype="error_max_turns", is_error=True)],
        error=ResultError("Claude Code returned an error result", exit_code=1),
    )
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AgentStepExecutionError, match="error_max_turns"):
            await _agent(max_turns=1)(a=2, b=3)

    # The final trace keeps the session's details instead of an unavailable trace.
    assert observed[-1].kind == "trace_finished"
    header = observed[-1].header
    assert (header.status, header.stop_reason) == ("failed", "error_max_turns")
    assert observed[-1].header.native.result_subtype == "error_max_turns"


@pytest.mark.asyncio
async def test_api_error_result_fails_with_the_api_error(replay):
    # The SDK reports API failures as a "success" result that is an error.
    replay(
        [_init(), _result(None, is_error=True, result="API Error: 529 overloaded")],
        error=ResultError("Claude Code returned an error result", exit_code=1),
    )

    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AgentStepExecutionError, match="529"):
            await _agent()(a=2, b=3)

    # The reason also reaches the final event's header.
    assert any("529" in error for error in observed[-1].header.native.errors)
    assert observed[-1].outputs is None


@pytest.mark.asyncio
async def test_many_long_result_errors_stay_within_the_trace_header_limit(replay):
    replay(
        # Three-byte characters are the worst case for the header's byte limit.
        [_init(), _result(None, is_error=True, errors=["過負荷" * 2000] * 100)],
        error=ResultError("Claude Code returned an error result", exit_code=1),
    )

    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AgentStepExecutionError, match="過負荷"):
            await _agent()(a=2, b=3)

    # The header travels in every run snapshot; its errors are cut, not dropped.
    errors = observed[-1].header.native.errors
    assert errors and all(error.startswith("過負荷") for error in errors)


@pytest.mark.asyncio
async def test_wrongly_typed_structured_output_is_an_output_error(replay):
    replay(_success_stream(total="fifteen"))

    with pytest.raises(AgentOutputError, match="total"):
        await _agent()(a=2, b=3)


@pytest.mark.asyncio
async def test_cancellation_reports_an_unavailable_trace_and_propagates(replay):
    cancelled = asyncio.CancelledError("stopped")
    replay([_init()], error=cancelled)
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(asyncio.CancelledError) as raised:
            await _agent()(a=2, b=3)

    assert raised.value is cancelled
    final = observed[-1]
    assert (final.kind, final.status, final.external_run_id) == (
        "trace_unavailable",
        "cancelled",
        "session-1",
    )


@pytest.mark.asyncio
async def test_a_missing_claude_cli_names_what_to_install(replay):
    replay([], error=CLINotFoundError("claude not found"))
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AgentStepExecutionError, match="Ray worker"):
            await _agent()(a=2, b=3)

    assert observed[-1].kind == "trace_unavailable"


class Edit(ava.Signature):
    """Write notes.txt into the workspace."""

    workspace: ava.Workspace = ava.InputField()
    document: ava.File = ava.InputField()
    edited: ava.Workspace = ava.OutputField()
    summary: str = ava.OutputField()


@pytest.mark.asyncio
async def test_a_workspace_is_the_working_directory_and_comes_back_as_an_output(
    monkeypatch, tmp_path
):
    seen: dict[str, object] = {}
    (tmp_path / "existing.txt").write_text("kept")
    (tmp_path / "draft.txt").write_text("draft")
    workspace = ava.Workspace.from_directory(tmp_path, reference="notes")

    async def edit_then_finish(*, prompt, options):
        seen.update(prompt=prompt, options=options)
        (tmp_path / "notes.txt").write_text("written by the agent")
        for message in [_init(), _result({"summary": "done"})]:
            yield message

    monkeypatch.setattr(sdk_module, "_session", edit_then_finish)
    result = await ava.Agent(
        signature=Edit, step_name="edit", runtime=ClaudeAgent(model=MODEL)
    )(workspace=workspace, document=ava.File.from_path(tmp_path / "draft.txt"))

    options = seen["options"]
    assert options.cwd == str(tmp_path)
    # A file inside the working directory is named by its relative path, never copied.
    assert '"document": "draft.txt"' in seen["prompt"]
    assert options.add_dirs == []
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "draft.txt",
        "existing.txt",
        "notes.txt",
    ]
    assert "edited" not in options.output_format["schema"]["properties"]
    assert result.outputs.summary == "done"
    assert result.outputs.edited.reference == "notes"
    # The returned reference stays bound to the directory the agent worked in.
    assert result.outputs.edited.path == tmp_path


class Documents(ava.Signature):
    documents: list[ava.File] = ava.InputField()
    answer: str = ava.OutputField()


@pytest.mark.asyncio
async def test_input_files_are_read_where_they_are_and_traced_by_reference(replay, tmp_path):
    seen = replay([_init(), _result({"answer": "read"})])
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "a.txt").write_text("a")
    (sources / "b.txt").write_text("b")
    workspace = ava.Workspace.from_directory(sources, reference="snapshot:sources")
    documents = [
        ava.File.from_path(sources / name, workspace=workspace) for name in ("a.txt", "b.txt")
    ]

    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await ava.Agent(signature=Documents, step_name="docs", runtime=ClaudeAgent())(
            documents=documents
        )

    assert result.outputs.answer == "read"
    # Files outside the working directory keep their host path, and Claude may reach
    # their folder; nothing is copied.
    assert str(sources / "a.txt") in seen["prompt"]
    assert str(sources / "b.txt") in seen["prompt"]
    assert seen["options"].add_dirs == [str(sources)]
    assert sorted(path.name for path in sources.iterdir()) == ["a.txt", "b.txt"]
    # The trace records each file by its reference, never by its host path.
    started = observed[0].common
    assert isinstance(started, RunStarted)
    assert [item["path"] for item in started.inputs["documents"]] == ["a.txt", "b.txt"]
    assert {item["workspace"] for item in started.inputs["documents"]} == {"snapshot:sources"}
    assert str(tmp_path) not in started.model_dump_json()


class Report(ava.Signature):
    question: str = ava.InputField()
    report: ava.File = ava.OutputField()
    appendices: list[ava.File] = ava.OutputField()


@pytest.mark.asyncio
async def test_file_outputs_are_reserved_in_the_run_workspace(monkeypatch, tmp_path):
    seen: dict[str, object] = {}
    workspace = ava.Workspace.from_directory(tmp_path, reference="snapshot:run")

    async def write_then_finish(*, prompt, options):
        seen.update(prompt=prompt, options=options)
        (folder,) = options.add_dirs
        (Path(folder) / "report.txt").write_text("report")
        (Path(folder) / "a.txt").write_text("a")
        result = {
            "report": f"{folder}/report.txt",
            "appendices": [f"{folder}/a.txt"],
        }
        for message in [_init(), _result(result)]:
            yield message

    monkeypatch.setattr(sdk_module, "_session", write_then_finish)
    observed = []
    with (
        ava.file_reference_context(workspace, attempt="attempt-1"),
        capture_agent_evidence(observed.append, errors="raise"),
    ):
        result = await ava.Agent(signature=Report, step_name="report", runtime=ClaudeAgent())(
            question="q"
        )

    (folder,) = seen["options"].add_dirs
    assert Path(folder).is_relative_to(tmp_path / ".outputs" / "report" / "attempt-1")
    assert f"Write every file output inside {folder}" in seen["prompt"]
    schema = seen["options"].output_format["schema"]["properties"]
    assert schema["report"]["type"] == "string"
    assert schema["appendices"]["items"]["type"] == "string"
    report = result.outputs.report
    assert report.read_bytes() == b"report"
    assert report.ref.workspace == "snapshot:run"
    assert report.ref.path.startswith(".outputs/report/attempt-1/")
    assert [file.read_bytes() for file in result.outputs.appendices] == [b"a"]
    # The final event keeps each file output by its reference, never its host path.
    outputs = observed[-1].outputs
    assert (outputs["report"]["kind"], outputs["report"]["path"]) == ("file", report.ref.path)
    assert [item["kind"] for item in outputs["appendices"]] == ["file"]
    assert str(tmp_path) not in observed[-1].model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report", [None, "missing.txt", __file__], ids=["absent", "missing", "outside"]
)
async def test_file_outputs_that_cannot_be_used_are_output_errors(replay, tmp_path, report):
    replay([_init(), _result({"report": report, "appendices": []})])

    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AgentOutputError, match="report"):
            await ava.Agent(signature=Report, step_name="report", runtime=ClaudeAgent())(
                question="q"
            )
    # The step still sends its one final event, with no outputs to show.
    assert (observed[-1].kind, observed[-1].outputs) == ("trace_finished", None)


class TwoWorkspaces(ava.Signature):
    first: ava.Workspace = ava.InputField()
    second: ava.Workspace = ava.InputField()
    answer: str = ava.OutputField()


class Attachment(BaseModel):
    file: ava.File


class FileInModelOutput(ava.Signature):
    question: str = ava.InputField()
    attachment: Attachment = ava.OutputField()


class FileTupleInput(ava.Signature):
    files: tuple[ava.File, ...] = ava.InputField()
    answer: str = ava.OutputField()


class FileInModelInput(ava.Signature):
    attachment: Attachment = ava.InputField()
    answer: str = ava.OutputField()


class AnnotatedFileListInput(ava.Signature):
    files: list[Annotated[ava.File, Field(description="a file")]] = ava.InputField()
    answer: str = ava.OutputField()


class OptionalWorkspaceInput(ava.Signature):
    workspace: ava.Workspace | None = ava.InputField()
    answer: str = ava.OutputField()


class OptionalWorkspaceOutput(ava.Signature):
    question: str = ava.InputField()
    workspace: ava.Workspace | None = ava.OutputField()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("signature", "inputs", "match"),
    [
        (TwoWorkspaces, {"first": ava.Workspace(), "second": ava.Workspace()}, "at most one"),
        (FileInModelOutput, {"question": "q"}, "typed exactly"),
        (OptionalWorkspaceOutput, {"question": "q"}, "typed exactly"),
        (FileTupleInput, {"files": (ava.File(content=b"x"),)}, "typed exactly"),
        (
            FileInModelInput,
            {"attachment": Attachment(file=ava.File(content=b"x"))},
            "typed exactly",
        ),
        (OptionalWorkspaceInput, {"workspace": None}, "typed exactly"),
        (AnnotatedFileListInput, {"files": [ava.File(content=b"x")]}, "typed exactly"),
    ],
    ids=[
        "two-workspaces",
        "file-in-model-output",
        "optional-workspace-output",
        "file-tuple-input",
        "file-in-model-input",
        "optional-workspace-input",
        "annotated-file-list-input",
    ],
)
async def test_unsupported_signatures_fail_before_calling_claude(
    replay, signature, inputs, match
):
    seen = replay([])

    with pytest.raises(AgentStepError, match=match):
        await ava.Agent(signature=signature, step_name="x", runtime=ClaudeAgent())(**inputs)
    assert seen == {}


@pytest.mark.asyncio
async def test_step_tools_run_through_the_in_process_tool_server():
    def scale(value: int, factor: int = 2) -> int:
        """Multiply value by factor."""
        if value < 0:
            raise ValueError("negative")
        return value * factor

    sdk_tool = sdk_module._sdk_tool(
        ToolSpec(name="scale", description="Multiply.", function=scale)
    )

    assert await sdk_tool.handler({"value": 4}) == {"content": [{"type": "text", "text": "8"}]}
    failed = await sdk_tool.handler({"value": -1})
    assert failed["is_error"] is True
    assert "negative" in failed["content"][0]["text"]
    assert sdk_tool.input_schema["required"] == ["value"]


def test_declaration_and_merging_follow_the_runtime_rules():
    step = ClaudeAgent(max_turns=3)
    default = ClaudeAgent(model=MODEL, builtin_tools=["Read"])

    assert step.merged_over(default) == ClaudeAgent(
        model=MODEL, max_turns=3, builtin_tools=["Read"]
    )
    assert step.declaration(workflow_default=default) == ClaudeAgentDeclaration(
        model=MODEL,
        settings={
            "max_turns": 3,
            "permission_mode": "default",
            "system_prompt_append": None,
            "max_budget_usd": None,
            "narrate": True,
        },
        builtin_tools=("Read",),
    )


def test_a_step_declares_its_claude_runtime():
    @ava.agent_step(Total, runtime=ClaudeAgent(model=MODEL))
    async def total(a: int, b: int, *, agent: ava.Agent) -> int:
        return (await agent(a=a, b=b)).outputs.total

    declaration = total.fn.__agent_step__.declaration()
    assert declaration.runtime.kind == "claude_agent"
    assert declaration.runtime.model == MODEL


@pytest.mark.asyncio
async def test_without_the_sdk_the_step_says_which_extra_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    monkeypatch.delitem(sys.modules, "avalanche.runtimes.claude_agent._sdk")
    monkeypatch.delattr(importlib.import_module("avalanche.runtimes.claude_agent"), "_sdk")

    with pytest.raises(AgentStepExecutionError, match=r"avalanche-ai\[claude\]"):
        await _agent()(a=2, b=3)


@pytest.mark.skipif(
    os.environ.get("AVALANCHE_LIVE_CLAUDE") != "1",
    reason="set AVALANCHE_LIVE_CLAUDE=1 to call Claude through the local claude CLI",
)
@pytest.mark.asyncio
async def test_live_claude_call_with_a_small_model():
    class Sum(ava.Signature):
        """Add a and b."""

        a: int = ava.InputField()
        b: int = ava.InputField()
        total: int = ava.OutputField()

    agent = ava.Agent(
        signature=Sum, step_name="sum", runtime=ClaudeAgent(model=MODEL, max_turns=4)
    )
    result = await agent(a=2, b=3)

    assert result.outputs.total == 5
    assert (result.trace.header.runtime_kind, result.trace.header.status) == (
        "claude_agent",
        "succeeded",
    )


@pytest.mark.skipif(
    os.environ.get("AVALANCHE_LIVE_CLAUDE") != "1",
    reason="set AVALANCHE_LIVE_CLAUDE=1 to call Claude through the local claude CLI",
)
@pytest.mark.asyncio
async def test_live_claude_reads_an_outside_file_and_writes_a_file_output(tmp_path):
    class Shout(ava.Signature):
        """Read the input file and write its text in upper case to a new file."""

        document: ava.File = ava.InputField()
        shouted: ava.File = ava.OutputField()

    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "note.txt").write_text("quiet words")
    (tmp_path / "run").mkdir()
    run_workspace = ava.Workspace.from_directory(tmp_path / "run", reference="snapshot:run")
    agent = ava.Agent(
        signature=Shout,
        step_name="shout",
        runtime=ClaudeAgent(
            model=MODEL,
            max_turns=8,
            builtin_tools=("Read", "Write"),
            permission_mode="acceptEdits",
        ),
    )
    with ava.file_reference_context(run_workspace, attempt="attempt-1"):
        result = await agent(document=ava.File.from_path(sources / "note.txt"))

    shouted = result.outputs.shouted
    assert shouted.ref.path.startswith(".outputs/shout/attempt-1/")
    assert shouted.read_bytes().decode().strip() == "QUIET WORDS"


@pytest.mark.skipif(
    os.environ.get("AVALANCHE_LIVE_CLAUDE") != "1",
    reason="set AVALANCHE_LIVE_CLAUDE=1 to call Claude through the local claude CLI",
)
@pytest.mark.asyncio
async def test_live_claude_uses_a_step_skill_and_reads_its_file(tmp_path):
    class CodeWord(ava.Signature):
        """Give the code word of the project named in the input."""

        project: str = ava.InputField()
        code_word: str = ava.OutputField()

    codes = tmp_path / "codes.md"
    codes.write_text("The code word of project heron is PELICAN-4217.\n")
    skill = ava.agent.Skill(
        name="project-codes",
        description="Looks up project code words. Use when asked for a project's code word.",
        instructions="Read references/codes.md in this skill's folder and answer from it.",
        files={"references/codes.md": codes},
    )
    agent = ava.Agent(
        signature=CodeWord,
        step_name="code_word",
        runtime=ClaudeAgent(model=MODEL, max_turns=8),
        skills=(skill,),
    )
    result = await agent(project="heron")

    assert result.outputs.code_word.strip() == "PELICAN-4217"
    calls = [call.name for turn in result.trace.turns for call in turn.tool_calls]
    assert "Skill" in calls


@pytest.mark.asyncio
async def test_a_response_split_across_messages_and_subagent_traffic_form_one_turn(replay):
    subagent = AssistantMessage(
        content=[ToolUseBlock(id="sub-1", name="Read", input={"path": "x"})],
        model=MODEL,
        parent_tool_use_id="call-2",
        message_id="sub-msg",
    )
    replay(
        [
            _init(),
            # The CLI streams one response with two parallel tool calls as two messages.
            _assistant(
                ToolUseBlock(
                    id="call-1", name="mcp__avalanche__lookup_bonus", input={"name": "a"}
                ),
                message_id="parallel",
            ),
            _assistant(
                ToolUseBlock(id="call-2", name="Task", input={"prompt": "check"}),
                message_id="parallel",
            ),
            subagent,
            UserMessage(
                content=[ToolResultBlock(tool_use_id="sub-1", content="file")],
                parent_tool_use_id="call-2",
            ),
            UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="call-1", content="10"),
                    ToolResultBlock(tool_use_id="call-2", content="checked"),
                ]
            ),
            _assistant(TextBlock(text="Done."), usage={"input_tokens": 7, "output_tokens": 9}),
            _result({"total": 15}),
        ]
    )
    result = await _agent()(a=2, b=3)

    first, second = result.trace.turns
    assert [(call.call_id, call.result) for call in first.tool_calls] == [
        ("call-1", "10"),
        ("call-2", "checked"),
    ]
    assert second.message == "Done."
    assert result.trace.header.turn_count == 2


@pytest.mark.asyncio
async def test_the_message_stream_is_closed_when_handling_a_message_fails(monkeypatch):
    closed = []

    async def query(*, prompt, options):
        try:
            yield _init()
            yield UserMessage(content=[ToolResultBlock(tool_use_id="nowhere", content="?")])
            yield _result({"total": 1})
        finally:
            closed.append(True)

    monkeypatch.setattr(sdk_module, "_session", query)
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AgentStepExecutionError, match="unknown tool call"):
            await _agent()(a=2, b=3)

    assert closed == [True]
    assert observed[-1].kind == "trace_unavailable"


@pytest.mark.asyncio
async def test_the_cli_has_stopped_before_its_working_folder_is_removed(monkeypatch):
    seen = {}

    class Client:
        def __init__(self, *, options):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            seen["cwd"] = Path(self.options.cwd)
            seen["cwd_existed_at_disconnect"] = seen["cwd"].is_dir()

        async def query(self, prompt):
            pass

        async def receive_response(self):
            yield _init()
            yield UserMessage(content=[ToolResultBlock(tool_use_id="nowhere", content="?")])
            yield _result({"total": 1})

    monkeypatch.setattr(sdk_module, "ClaudeSDKClient", Client)
    with capture_agent_evidence(lambda _: None, errors="raise"):
        with pytest.raises(AgentStepExecutionError, match="unknown tool call"):
            await _agent()(a=2, b=3)

    assert seen["cwd_existed_at_disconnect"]
    assert not seen["cwd"].exists()


@pytest.mark.asyncio
async def test_a_setup_failure_writes_nothing_into_the_workspace(replay, tmp_path):
    def untyped(value):
        return value

    seen = replay([])
    directory = tmp_path / "workspace"
    directory.mkdir()
    workspace = ava.Workspace.from_directory(directory, reference="notes")
    (tmp_path / "draft.txt").write_text("draft")

    with pytest.raises(AgentStepError, match="type annotation"):
        await ava.Agent(
            signature=Edit, step_name="edit", runtime=ClaudeAgent(), tools=(untyped,)
        )(workspace=workspace, document=ava.File.from_path(tmp_path / "draft.txt"))

    assert seen == {}
    assert list(directory.iterdir()) == []


def _skill(tmp_path: Path, name: str = "bonus-rules", **fields) -> ava.agent.Skill:
    rules = tmp_path / f"{name}.md"
    rules.write_text("Bonuses are ten points.")
    return ava.agent.Skill(
        name=name,
        description="Explains bonuses. Use when adding a bonus.",
        instructions="Read references/rules.md before adding a bonus.",
        files={"references/rules.md": rules},
        **fields,
    )


def _plugin_files(plugin: Path) -> dict[str, str]:
    return {
        path.relative_to(plugin).as_posix(): path.read_text()
        for path in sorted(plugin.rglob("*"))
        if path.is_file()
    }


@pytest.mark.asyncio
async def test_step_skills_run_from_a_plugin_made_for_the_call(monkeypatch, tmp_path):
    script = tmp_path / "check.sh"
    script.write_text("#!/bin/sh\necho ok\n")
    script.chmod(0o755)
    skill = _skill(tmp_path, metadata={"owner": "finance"})
    skill = skill.model_copy(update={"files": {**skill.files, "scripts/check.sh": script}})
    seen: dict[str, object] = {}

    async def query(*, prompt, options):
        plugin = Path(options.plugins[0]["path"])
        seen.update(
            options=options,
            plugin=plugin,
            files=_plugin_files(plugin),
            script_mode=(plugin / "skills/bonus-rules/scripts/check.sh").stat().st_mode,
        )
        for message in _success_stream():
            yield message

    monkeypatch.setattr(sdk_module, "_session", query)
    agent = ava.Agent(
        signature=Total,
        step_name="total",
        runtime=ClaudeAgent(model=MODEL),
        tools=(lookup_bonus,),
        skills=(skill,),
    )
    result = await agent(a=2, b=3)

    assert result.outputs.total == 15
    options, plugin = seen["options"], seen["plugin"]
    assert options.plugins == [{"type": "local", "path": str(plugin)}]
    assert options.skills == ["avalanche:bonus-rules"]
    assert options.tools == ["Skill", "Read"]
    assert f"Read(/{plugin}/**)" in options.allowed_tools
    [matcher] = options.hooks["PreToolUse"]
    assert matcher.matcher == "Read"
    assert "Skill" not in options.allowed_tools
    assert str(plugin) in options.add_dirs
    # The user's and project's Claude settings and MCP servers stay out.
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    files = seen["files"]
    assert set(files) == {
        ".claude-plugin/plugin.json",
        "skills/bonus-rules/SKILL.md",
        "skills/bonus-rules/references/rules.md",
        "skills/bonus-rules/scripts/check.sh",
    }
    assert files["skills/bonus-rules/references/rules.md"] == "Bonuses are ten points."
    assert seen["script_mode"] & 0o111 == 0o111
    written = tmp_path / "written" / "bonus-rules"
    written.mkdir(parents=True)
    for relative, text in files.items():
        if relative.startswith("skills/bonus-rules/"):
            target = written / relative.removeprefix("skills/bonus-rules/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
    reloaded = ava.agent.Skill.from_dir(written)
    assert reloaded.model_dump(exclude={"files"}) == skill.model_dump(exclude={"files"})
    assert not plugin.exists()


@pytest.mark.asyncio
async def test_skills_without_files_add_only_the_skill_tool(replay, tmp_path):
    seen = replay(_success_stream())
    skill = ava.agent.Skill(
        name="bonus-rules", description="Explains bonuses.", instructions="Add ten."
    )
    agent = ava.Agent(
        signature=Total,
        step_name="total",
        runtime=ClaudeAgent(model=MODEL, builtin_tools=("Bash",)),
        tools=(lookup_bonus,),
        skills=(skill,),
    )
    await agent(a=2, b=3)

    options = seen["options"]
    assert options.tools == ["Bash", "Skill"]
    assert not any(rule.startswith("Read") for rule in options.allowed_tools)
    assert options.hooks is None


@pytest.mark.asyncio
async def test_a_step_without_skills_has_no_skill_tool_or_plugin(replay):
    seen = replay(_success_stream())
    await _agent()(a=2, b=3)

    options = seen["options"]
    assert (options.plugins, options.skills, options.tools) == ([], None, [])


@pytest.mark.parametrize(
    "error", [RuntimeError("CLI crashed"), asyncio.CancelledError()], ids=["fails", "cancelled"]
)
@pytest.mark.asyncio
async def test_the_skill_plugin_is_removed_when_the_call_fails(monkeypatch, tmp_path, error):
    plugins: list[Path] = []

    async def query(*, prompt, options):
        plugins.append(Path(options.plugins[0]["path"]))
        yield _init()
        raise error

    monkeypatch.setattr(sdk_module, "_session", query)
    agent = ava.Agent(
        signature=Total,
        step_name="total",
        runtime=ClaudeAgent(model=MODEL),
        tools=(lookup_bonus,),
        skills=(_skill(tmp_path),),
    )
    with pytest.raises((AgentStepExecutionError, asyncio.CancelledError)):
        await agent(a=2, b=3)

    assert plugins and not plugins[0].exists()
    assert not plugins[0].parent.exists()


def test_a_claude_step_declares_its_skills(tmp_path):
    @ava.agent_step(Total, runtime=ClaudeAgent(model=MODEL), skills=[_skill(tmp_path)])
    async def total(a: int, b: int, *, agent: ava.Agent) -> int:
        return (await agent(a=a, b=b)).outputs.total

    declaration = total.fn.__agent_step__.declaration()

    assert [(skill.name, skill.files) for skill in declaration.skills] == [
        ("bonus-rules", ("references/rules.md",))
    ]
    assert declaration.skills[0].description.startswith("Explains bonuses")


@pytest.mark.parametrize("permission_mode", ["default", "bypassPermissions"])
@pytest.mark.asyncio
async def test_the_added_read_tool_reads_only_inside_the_skill_plugin(
    monkeypatch, tmp_path, permission_mode
):
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    decisions: dict[str, str] = {}

    async def query(*, prompt, options):
        # The hook is the same in every mode; that the CLI runs hooks before its
        # permission checks, bypassPermissions included, is the CLI's contract.
        assert options.permission_mode == permission_mode
        plugin = Path(options.plugins[0]["path"])
        [matcher] = options.hooks["PreToolUse"]
        [hook] = matcher.hooks
        reads = {
            "skill file": str(plugin / "skills/bonus-rules/references/rules.md"),
            "dotted skill file": str(plugin / "skills/../skills/bonus-rules/SKILL.md"),
            "outside file": str(secret),
            "escape": str(plugin / ".." / ".." / "secret.txt"),
            "working directory": "notes.txt",
        }
        for name, file_path in reads.items():
            output = await hook(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "session-1",
                    "transcript_path": "",
                    "cwd": options.cwd,
                    "tool_name": "Read",
                    "tool_input": {"file_path": file_path},
                    "tool_use_id": "call-1",
                },
                "call-1",
                {"signal": None},
            )
            decisions[name] = output.get("hookSpecificOutput", {}).get(
                "permissionDecision", "allow"
            )
        for message in _success_stream():
            yield message

    monkeypatch.setattr(sdk_module, "_session", query)
    agent = ava.Agent(
        signature=Total,
        step_name="total",
        runtime=ClaudeAgent(model=MODEL, permission_mode=permission_mode),
        tools=(lookup_bonus,),
        skills=(_skill(tmp_path),),
    )
    await agent(a=2, b=3)

    assert decisions == {
        "skill file": "allow",
        "dotted skill file": "allow",
        "outside file": "deny",
        "escape": "deny",
        "working directory": "deny",
    }


@pytest.mark.asyncio
async def test_a_listed_read_tool_keeps_its_own_scope(replay, tmp_path):
    seen = replay(_success_stream())
    agent = ava.Agent(
        signature=Total,
        step_name="total",
        runtime=ClaudeAgent(model=MODEL, builtin_tools=("Read",)),
        tools=(lookup_bonus,),
        skills=(_skill(tmp_path),),
    )
    await agent(a=2, b=3)

    options = seen["options"]
    assert (options.tools, options.hooks) == (["Read", "Skill"], None)


def _hook_matcher(matcher: str):
    from claude_agent_sdk import HookMatcher

    async def allow(hook_input, tool_use_id, context):
        return {}

    return HookMatcher(matcher=matcher, hooks=[allow])


@pytest.mark.asyncio
async def test_sdk_options_reach_claude_and_replace_avalanches_values(replay):
    seen = replay(_success_stream())
    await _agent(
        effort="low",
        fallback_model="claude-sonnet-5-5",
        env={"DEBUG": "1"},
        setting_sources=["project"],
        strict_mcp_config=False,
    )(a=2, b=3)

    options = seen["options"]
    assert (options.effort, options.fallback_model, options.env) == (
        "low",
        "claude-sonnet-5-5",
        {"DEBUG": "1"},
    )
    assert (options.setting_sources, options.strict_mcp_config) == (["project"], False)
    assert options.model == MODEL


@pytest.mark.asyncio
async def test_collections_avalanche_fills_are_combined_with_the_users(replay, tmp_path):
    seen = replay(_success_stream())
    docs = {"type": "http", "url": "https://example.test/mcp"}
    script = tmp_path / "check.sh"
    script.write_text("#!/bin/sh\n")
    skill = _skill(tmp_path)
    skill = skill.model_copy(update={"files": {**skill.files, "scripts/check.sh": script}})
    user_hook = _hook_matcher("Bash")
    stop_hook = _hook_matcher("")
    agent = ava.Agent(
        signature=Total,
        step_name="total",
        runtime=ClaudeAgent(
            model=MODEL,
            mcp_servers={"docs": docs},
            hooks={"PreToolUse": [user_hook], "Stop": [stop_hook]},
            plugins=[{"type": "local", "path": "/plugins/mine"}],
            allowed_tools=["mcp__docs__search", "mcp__avalanche__lookup_bonus"],
            add_dirs=["/data/reference"],
            tools=["Grep"],
            skills=["mine:review"],
        ),
        tools=(lookup_bonus,),
        skills=(skill,),
    )
    await agent(a=2, b=3)

    options = seen["options"]
    assert set(options.mcp_servers) == {"avalanche", "docs"}
    assert options.mcp_servers["docs"] == docs
    guard, added = options.hooks["PreToolUse"]
    assert (guard.matcher, added) == ("Read", user_hook)
    assert options.hooks["Stop"] == [stop_hook]
    [avalanche_plugin, user_plugin] = options.plugins
    assert user_plugin == {"type": "local", "path": "/plugins/mine"}
    assert options.allowed_tools.count("mcp__avalanche__lookup_bonus") == 1
    assert "mcp__docs__search" in options.allowed_tools
    assert {avalanche_plugin["path"], "/data/reference"} <= set(options.add_dirs)
    assert options.tools == ["Skill", "Read", "Grep"]
    assert options.skills == ["avalanche:bonus-rules", "mine:review"]


@pytest.mark.parametrize(
    ("option", "value", "pointer"),
    [
        ("system_prompt", "Be brief.", "system_prompt_append"),
        ("output_format", {"type": "json_schema", "schema": {}}, "outputs"),
        ("cwd", "/tmp", "ava.Workspace"),
    ],
)
def test_options_avalanche_needs_for_the_answer_are_rejected(option, value, pointer):
    with pytest.raises(ValueError, match=pointer):
        ClaudeAgent(**{option: value})


def test_an_unknown_option_fails_when_the_runtime_is_built():
    with pytest.raises(ValueError, match="effortt"):
        ClaudeAgent(effortt="low")


@pytest.mark.asyncio
async def test_an_unknown_option_fails_the_step_when_the_sdk_was_missing_at_build(
    monkeypatch, replay
):
    runtime_module = importlib.import_module("avalanche.runtimes.claude_agent.runtime")
    monkeypatch.setattr(runtime_module, "_sdk_option_names", lambda: None)
    agent = _agent(effortt="low")
    seen = replay(_success_stream())

    with pytest.raises(AgentStepError, match="effortt"):
        await agent(a=2, b=3)
    assert "options" not in seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mcp_servers",
    [{"avalanche": {"type": "http", "url": "https://example.test"}}, "/etc/mcp.json"],
    ids=["reserved-name", "config-file"],
)
async def test_mcp_servers_that_cannot_be_combined_fail_the_step(replay, mcp_servers):
    seen = replay(_success_stream())

    with pytest.raises(AgentStepError, match="mcp_servers|avalanche"):
        await _agent(mcp_servers=mcp_servers)(a=2, b=3)
    assert "options" not in seen


def test_sdk_options_are_merged_by_name_and_never_declared():
    default = ClaudeAgent(model=MODEL, effort="high", env={"API_TOKEN": "secret"})
    step = ClaudeAgent(effort="low")

    merged = step.merged_over(default)
    assert merged.sdk_options == {"effort": "low", "env": {"API_TOKEN": "secret"}}
    declaration = step.declaration(workflow_default=default)
    assert "secret" not in declaration.model_dump_json()
    assert "effort" not in declaration.settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sdk_options", "guarded"),
    [
        ({"tools": ["Read"]}, False),
        ({"tools": {"type": "preset", "preset": "claude_code"}}, False),
        ({"allowed_tools": ["Read"]}, True),
    ],
    ids=["tools", "tools-preset", "allowed-only"],
)
async def test_only_a_read_enabled_in_sdk_tools_lifts_the_skill_folder_guard(
    replay, tmp_path, sdk_options, guarded
):
    seen = replay(_success_stream())
    script = tmp_path / "check.sh"
    script.write_text("#!/bin/sh\n")
    skill = _skill(tmp_path)
    skill = skill.model_copy(update={"files": {**skill.files, "scripts/check.sh": script}})
    agent = ava.Agent(
        signature=Total,
        step_name="total",
        runtime=ClaudeAgent(model=MODEL, **sdk_options),
        tools=(lookup_bonus,),
        skills=(skill,),
    )
    await agent(a=2, b=3)

    hooks = seen["options"].hooks
    assert (
        hooks is not None and [m.matcher for m in hooks["PreToolUse"]] == ["Read"]
    ) is guarded
