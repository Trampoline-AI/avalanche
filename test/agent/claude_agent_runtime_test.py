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
from avalanche.agent.contract import ToolSpec  # noqa: E402
from avalanche.agent.declaration import ClaudeAgentDeclaration  # noqa: E402
from avalanche.agent.native import claude_agent as claude_native  # noqa: E402
from avalanche.agent.trace import ToolCallAction  # noqa: E402
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
        UserMessage(content=[ToolResultBlock(tool_use_id="call-2", content="ok")]),
        _result({"total": total}),
    ]


@pytest.fixture
def replay(monkeypatch):
    """Replace the SDK's ``query`` with a recorded stream; returns the options it saw."""
    seen: dict[str, object] = {}

    def install(messages, *, error: BaseException | None = None):
        async def query(*, prompt, options):
            seen.update(prompt=prompt, options=options)
            for message in messages:
                yield message
            if error is not None:
                raise error

        monkeypatch.setattr(sdk_module, "query", query)
        return seen

    return install


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
    assert (first.reasoning, first.text) == ("I need the bonus.", "Looking up the bonus.")
    (call,) = first.actions
    assert call == ToolCallAction(
        call_id="call-1",
        caller="model",
        name="mcp__avalanche__lookup_bonus",
        arguments={"name": "a"},
        result="10",
        error=None,
        duration_ms=None,
    )
    assert [action.name for action in second.actions] == ["StructuredOutput"]
    assert [
        (event.kind, event.native.kind if event.kind == "event" else None) for event in observed
    ] == [
        ("event", "started"),
        ("event", "session.initialized"),
        ("event", "tool.used"),
        ("event", "tool.returned"),
        ("event", "message.finished"),
        ("event", "tool.used"),
        ("event", "tool.returned"),
        ("event", "message.finished"),
        ("event", "finished"),
        ("trace_finished", None),
    ]
    assert observed[-1].record.turns == ()
    assert observed[0].native.inputs == {"a": 2, "b": 3}

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
    assert "Add the numbers" in options.system_prompt
    assert '"a": 2' in seen["prompt"]


class Note(ava.Signature):
    text: str = ava.InputField()
    answer: str = ava.OutputField()


@pytest.mark.asyncio
async def test_large_inputs_and_tool_calls_stay_within_the_live_event_limits(
    replay, monkeypatch
):
    monkeypatch.setattr(sdk_module, "_MAX_VALUE_BYTES", 64)
    monkeypatch.setattr(sdk_module, "_MAX_TURN_ACTION_BYTES", 1)
    seen = replay(_success_stream())
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await _agent()(a=2, b=3)
    turns = [
        e.common for e in observed if e.kind == "event" and e.native.kind == "message.finished"
    ]
    # Over the budget, a live turn's calls are counted as omitted; the record keeps them.
    assert [(len(t.turn.actions), t.omitted_tool_calls) for t in turns] == [(0, 1), (0, 1)]
    assert [len(turn.actions) for turn in result.trace.turns] == [1, 1]

    seen = replay([_init(), _result({"answer": "ok"})])
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        await ava.Agent(signature=Note, step_name="note", runtime=ClaudeAgent())(text="x" * 500)
    # Claude gets the whole input; the started event keeps only a marker for it.
    assert "x" * 500 in seen["prompt"]
    assert observed[0].native.inputs["text"]["kind"] == "unavailable"


@pytest.mark.asyncio
async def test_a_failed_tool_is_kept_on_its_call_and_does_not_fail_the_turn(replay):
    replay(_success_stream(tool_error=True))
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await _agent()(a=2, b=3)

    (call,) = result.trace.turns[0].actions
    assert (call.result, call.error) == (None, "lookup failed")
    turn_event = next(
        e for e in observed if e.kind == "event" and e.native.kind == "message.finished"
    )
    assert turn_event.failed is False


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
    assert (final.record.header.status, final.record.header.stop_reason) == (
        "failed",
        stop_reason,
    )


def test_a_failed_result_without_a_reason_names_its_subtype():
    finished = claude_native.Finished(
        subtype="error_during_execution",
        is_error=True,
        num_turns=1,
        structured_output=None,
        output_omitted=False,
        workspace_outputs=(),
        errors=(),
    )
    assert finished.error == "error_during_execution"


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
    header = observed[-1].record.header
    assert (header.status, header.stop_reason) == ("failed", "error_max_turns")
    assert observed[-1].record.native.result_subtype == "error_max_turns"


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

    # The reason also reaches the live result event and the final trace.
    finished = next(e for e in observed if e.kind == "event" and e.native.kind == "finished")
    assert "529" in finished.native.error
    assert any("529" in error for error in observed[-1].record.native.errors)


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

    monkeypatch.setattr(sdk_module, "query", edit_then_finish)
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
    started = observed[0].native
    assert isinstance(started, claude_native.Started)
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

    monkeypatch.setattr(sdk_module, "query", write_then_finish)
    with ava.file_reference_context(workspace, attempt="attempt-1"):
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report", [None, "missing.txt", __file__], ids=["absent", "missing", "outside"]
)
async def test_file_outputs_that_cannot_be_used_are_output_errors(replay, tmp_path, report):
    replay([_init(), _result({"report": report, "appendices": []})])

    with pytest.raises(AgentOutputError, match="report"):
        await ava.Agent(signature=Report, step_name="report", runtime=ClaudeAgent())(
            question="q"
        )


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
    assert [(call.call_id, call.result) for call in first.actions] == [
        ("call-1", "10"),
        ("call-2", "checked"),
    ]
    assert second.text == "Done."
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

    monkeypatch.setattr(sdk_module, "query", query)
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AgentStepExecutionError, match="unknown tool call"):
            await _agent()(a=2, b=3)

    assert closed == [True]
    assert observed[-1].kind == "trace_unavailable"


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
