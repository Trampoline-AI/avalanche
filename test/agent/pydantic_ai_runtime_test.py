"""The Pydantic AI runtime, driven by scripted Pydantic AI function models."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent as PydanticAgent  # noqa: E402
from pydantic_ai.capabilities import Capability  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    BinaryContent,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402
from pydantic_ai.usage import RequestUsage  # noqa: E402

import avalanche as ava  # noqa: E402
from avalanche._agent_evidence import capture_agent_evidence  # noqa: E402
from avalanche.agent import (  # noqa: E402
    AgentOutputError,
    AgentStepError,
    AgentStepExecutionError,
)
from avalanche.agent.declaration import PydanticAIDeclaration  # noqa: E402
from avalanche.agent.live import RunStarted, TurnFinished  # noqa: E402
from avalanche.agent.native.pydantic_ai import (  # noqa: E402
    PydanticAITraceHeader,
    PydanticAITurn,
    RunInitialized,
    ToolRetried,
)
from avalanche.agent.trace import TokenUsage, ToolCall  # noqa: E402
from avalanche.runtimes import PydanticAIAgent  # noqa: E402

sdk_module = importlib.import_module("avalanche.runtimes.pydantic_ai._sdk")

MODEL = "openai:gpt-5.4-nano"


class Total(ava.Signature):
    """Add the numbers and the bonus from lookup_bonus."""

    a: int = ava.InputField()
    b: int = ava.InputField()
    total: int = ava.OutputField()


def lookup_bonus(name: str) -> int:
    """Return the bonus for a name."""
    return 10


def _call(name, args, call_id):
    return ToolCallPart(name, args, tool_call_id=call_id)


def _response(*parts, **fields) -> ModelResponse:
    return ModelResponse(parts=list(parts), model_name="scripted", **fields)


def _answer(call_id="answer", **outputs) -> ModelResponse:
    return _response(_call("final_result", outputs, call_id), finish_reason="tool_call")


@pytest.fixture
def script(monkeypatch):
    """Run steps on a function model that answers with the given responses in order.

    Returns what the model saw: each request's messages and tools, and the agent's
    constructor arguments.
    """

    def install(*responses):
        seen: dict[str, list] = {"messages": [], "infos": [], "options": []}
        remaining = list(responses)

        def respond(messages, info: AgentInfo) -> ModelResponse:
            seen["messages"].append(messages)
            seen["infos"].append(info)
            step = remaining.pop(0)
            return step(messages, info) if callable(step) else step

        class Scripted(PydanticAgent):
            def __init__(self, model, **options):
                seen["options"].append({"model": model, **options})
                super().__init__(FunctionModel(respond), **options)

        monkeypatch.setattr(sdk_module, "Agent", Scripted)
        return seen

    return install


def _agent(**runtime) -> ava.Agent:
    return ava.Agent(
        signature=Total,
        step_name="total",
        runtime=PydanticAIAgent(model=MODEL, **runtime),
        tools=(lookup_bonus,),
    )


def _kinds(observed) -> list[tuple[str, str | None, str | None]]:
    return [
        (
            event.kind,
            event.native.kind if event.kind == "event" and event.native else None,
            event.common.kind if event.kind == "event" and event.common else None,
        )
        for event in observed
    ]


@pytest.mark.asyncio
async def test_a_run_maps_onto_outputs_turns_and_live_events(script):
    seen = script(
        _response(
            ThinkingPart("I need the bonus."),
            TextPart("Looking up the bonus."),
            _call("lookup_bonus", {"name": "a"}, "call-1"),
            usage=RequestUsage(input_tokens=30, output_tokens=5, cache_read_tokens=20),
            provider_name="openai",
            provider_response_id="resp-1",
            finish_reason="tool_call",
        ),
        _answer(total=15),
    )
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await _agent(max_turns=6, instructions_append="Be brief.")(a=2, b=3)

    assert result.outputs.total == 15
    header = result.trace.header
    assert (header.runtime_kind, header.status, header.stop_reason) == (
        "pydantic_ai",
        "succeeded",
        "final_result",
    )
    assert (header.turn_count, header.tool_call_count) == (2, 2)
    assert header.external_run_id is not None and header.session_id is not None
    assert header.native == PydanticAITraceHeader(requests=2, retries=0, error=None)
    first, second = result.trace.turns
    assert (first.reasoning, first.message) == ("I need the bonus.", "Looking up the bonus.")
    assert first.native == PydanticAITurn(
        model="function:respond:",
        provider="openai",
        provider_response_id="resp-1",
        finish_reason="tool_call",
    )
    # Pydantic AI counts cached tokens inside the input count; the trace keeps them apart.
    assert first.usage == TokenUsage(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=20,
        cache_creation_input_tokens=0,
        cost_usd=None,
    )
    (call,) = first.tool_calls
    assert call.model_copy(update={"duration_ms": None}) == ToolCall(
        call_id="call-1",
        caller="model",
        name="lookup_bonus",
        arguments={"name": "a"},
        result=10,
        error=None,
        duration_ms=None,
    )
    assert [call.name for call in second.tool_calls] == ["final_result"]
    assert _kinds(observed) == [
        ("event", None, "run.started"),
        ("event", "run.initialized", None),
        ("event", None, "tool.started"),
        ("event", None, "tool.finished"),
        ("event", None, "turn.finished"),
        ("event", None, "tool.started"),
        ("event", None, "tool.finished"),
        ("event", None, "turn.finished"),
        ("trace_finished", None, None),
    ]
    assert [event.turn for event in observed[2:8]] == [1, 1, 1, 2, 2, 2]
    initialized = observed[1].native
    assert isinstance(initialized, RunInitialized)
    assert (initialized.model, initialized.tools, initialized.skills) == (
        MODEL,
        ("lookup_bonus",),
        (),
    )
    assert initialized.run_id == header.external_run_id
    assert observed[0].common.inputs == {"a": 2, "b": 3}
    assert observed[-1].outputs == {"total": 15}

    (options,) = seen["options"]
    assert options["model"] == MODEL
    info = seen["infos"][0]
    assert info.instructions.startswith("Add the numbers and the bonus from lookup_bonus.")
    assert info.instructions.endswith("Be brief.")
    assert [tool.name for tool in info.function_tools] == ["lookup_bonus"]
    assert info.output_tools[0].parameters_json_schema["properties"]["total"]["type"] == (
        "integer"
    )
    (prompt,) = seen["messages"][0][0].parts
    assert isinstance(prompt, UserPromptPart)
    assert '"a": 2' in prompt.content[0]


@pytest.mark.asyncio
async def test_a_failing_tool_is_told_to_the_model_and_kept_on_its_call(script):
    def failing(name: str) -> int:
        """Fail."""
        raise ValueError("no bonus today")

    def after_failure(messages, info):
        (returned,) = messages[-1].parts
        assert "no bonus today" in returned.model_response_str()
        return _answer(total=5)

    script(_response(_call("failing", {"name": "a"}, "call-1")), after_failure)
    result = await ava.Agent(
        signature=Total,
        step_name="total",
        runtime=PydanticAIAgent(model=MODEL),
        tools=(failing,),
    )(a=2, b=3)

    assert result.outputs.total == 5
    (call,) = result.trace.turns[0].tool_calls
    assert call.result is None
    assert "ValueError: no bonus today" in call.error
    # The agent handled the failure, so the turn did not fail.
    assert not result.trace.turns[0].failed


@pytest.mark.asyncio
async def test_a_tool_raising_model_retry_asks_the_model_to_call_again(script):
    from pydantic_ai import ModelRetry

    def picky(name: str) -> int:
        """Accept only the name b."""
        if name != "b":
            raise ModelRetry("use the name b")
        return 10

    script(
        _response(_call("picky", {"name": "a"}, "first")),
        _response(_call("picky", {"name": "b"}, "second")),
        _answer(total=15),
    )
    result = await ava.Agent(
        signature=Total, step_name="total", runtime=PydanticAIAgent(model=MODEL), tools=(picky,)
    )(a=2, b=3)

    first, second, _ = result.trace.turns
    assert "use the name b" in first.tool_calls[0].error
    assert second.tool_calls[0].result == 10
    assert result.trace.header.native.retries == 1


@pytest.mark.asyncio
async def test_an_answer_that_fails_validation_is_retried_and_traced(script):
    script(_answer("bad", total="many"), _answer(total=5))
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await _agent()(a=2, b=3)

    assert result.outputs.total == 5
    assert result.trace.header.native.retries == 1
    (retried,) = result.trace.turns[0].tool_calls
    assert retried.error is not None and retried.result is None
    (retry,) = [e for e in observed if e.kind == "event" and isinstance(e.native, ToolRetried)]
    # The retry travels with the finished call it retries.
    assert retry.common.kind == "tool.finished"
    assert retry.common.error == retried.error
    assert (retry.native.call_id, retry.native.attempt) == ("bad", 1)


@pytest.mark.asyncio
async def test_a_run_pydantic_ai_ends_keeps_its_turns_in_a_failed_trace(script):
    script(_response(_call("lookup_bonus", {"name": "a"}, "call-1")))
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AgentStepExecutionError, match="UsageLimitExceeded"):
            await _agent(max_turns=1)(a=2, b=3)

    final = observed[-1]
    assert final.kind == "trace_finished"
    assert (final.header.status, final.header.stop_reason) == ("failed", "UsageLimitExceeded")
    assert final.header.native.error is not None
    assert final.header.turn_count == 1
    assert final.outputs is None


@pytest.mark.asyncio
async def test_an_unexpected_failure_reports_an_unavailable_trace(script):
    def broken(messages, info):
        raise RuntimeError("model adapter broke")

    script(broken)
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(AgentStepExecutionError, match="model adapter broke"):
            await _agent()(a=2, b=3)

    final = observed[-1]
    assert (final.kind, final.status) == ("trace_unavailable", "failed")
    assert final.external_run_id is not None


@pytest.mark.asyncio
async def test_cancellation_reports_an_unavailable_trace_and_propagates(script):
    cancelled = asyncio.CancelledError("stopped")

    def cancel(messages, info):
        raise cancelled

    script(cancel)
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(asyncio.CancelledError):
            await _agent()(a=2, b=3)

    final = observed[-1]
    assert (final.kind, final.status) == ("trace_unavailable", "cancelled")


@pytest.mark.asyncio
async def test_a_step_without_a_model_says_how_to_choose_one(script):
    script()
    with pytest.raises(AgentStepError, match="needs a model"):
        await ava.Agent(signature=Total, step_name="total", runtime=PydanticAIAgent())(a=2, b=3)


class Documents(ava.Signature):
    """Answer from the documents."""

    documents: list[ava.File] = ava.InputField()
    answer: str = ava.OutputField()


@pytest.mark.asyncio
async def test_input_files_are_attached_to_the_prompt_and_traced_by_reference(script, tmp_path):
    seen = script(_answer(answer="read"))
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4")
    workspace = ava.Workspace.from_directory(tmp_path, reference="snapshot:sources")
    documents = [
        ava.File.from_path(tmp_path / name, workspace=workspace) for name in ("a.txt", "b.pdf")
    ]
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await ava.Agent(
            signature=Documents, step_name="docs", runtime=PydanticAIAgent(model=MODEL)
        )(documents=documents)

    assert result.outputs.answer == "read"
    (prompt,) = seen["messages"][0][0].parts
    text, first_label, first, second_label, second = prompt.content
    assert '"attached file 1"' in text and '"attached file 2"' in text
    assert (first_label, second_label) == ("attached file 1: a.txt", "attached file 2: b.pdf")
    assert isinstance(first, BinaryContent) and isinstance(second, BinaryContent)
    assert (first.data, first.media_type) == (b"alpha", "text/plain")
    assert second.media_type == "application/pdf"
    started = observed[0].common
    assert isinstance(started, RunStarted)
    assert [item["path"] for item in started.inputs["documents"]] == ["a.txt", "b.pdf"]
    assert str(tmp_path) not in started.model_dump_json()


@pytest.mark.asyncio
async def test_a_file_pydantic_ai_cannot_send_fails_before_the_model_is_called(
    script, tmp_path
):
    seen = script()
    (tmp_path / "data.bin").write_bytes(b"\x00\x01")
    document = ava.File.from_path(tmp_path / "data.bin", content_type="application/x-thing")
    with pytest.raises(AgentStepError, match="application/x-thing"):
        await ava.Agent(
            signature=Documents, step_name="docs", runtime=PydanticAIAgent(model=MODEL)
        )(documents=[document])
    assert seen["messages"] == []


class Edit(ava.Signature):
    """Edit the notes in the workspace."""

    workspace: ava.Workspace = ava.InputField()
    edited: ava.Workspace = ava.OutputField()
    summary: str = ava.OutputField()


@pytest.mark.asyncio
async def test_a_workspace_is_worked_on_with_file_tools_and_comes_back(script, tmp_path):
    (tmp_path / "notes.txt").write_text("draft")
    workspace = ava.Workspace.from_directory(tmp_path, reference="notes")
    seen = script(
        # Pydantic AI runs one response's tool calls concurrently.
        _response(_call("list_files", {}, "list")),
        _response(
            _call("read_file", {"path": "notes.txt"}, "read"),
            _call("write_file", {"path": "out/final.txt", "text": "final"}, "write"),
            _call("read_file", {"path": "../escape.txt"}, "escape"),
        ),
        _answer(summary="done"),
    )
    result = await ava.Agent(
        signature=Edit, step_name="edit", runtime=PydanticAIAgent(model=MODEL)
    )(workspace=workspace)

    info = seen["infos"][0]
    assert [tool.name for tool in info.function_tools] == [
        "list_files",
        "read_file",
        "write_file",
    ]
    assert "edited" not in info.output_tools[0].parameters_json_schema["properties"]
    calls = {call.call_id: call for turn in result.trace.turns for call in turn.tool_calls}
    assert calls["list"].result == ["notes.txt"]
    assert calls["read"].result == "draft"
    assert "outside the working folder" in calls["escape"].error
    assert (tmp_path / "out" / "final.txt").read_text() == "final"
    assert result.outputs.summary == "done"
    assert result.outputs.edited.reference == "notes"
    assert result.outputs.edited.path == tmp_path


class Report(ava.Signature):
    """Write the report."""

    question: str = ava.InputField()
    report: ava.File = ava.OutputField()
    appendices: list[ava.File] = ava.OutputField()


@pytest.mark.asyncio
async def test_file_outputs_are_written_into_the_run_workspace(script, tmp_path):
    seen = script(
        _answer(
            report={"name": "report.md", "text": "# Report"},
            appendices=[{"name": "data/a.csv", "text": "a,b"}],
        )
    )
    run_workspace = ava.Workspace.from_directory(tmp_path, reference="snapshot:run")
    with ava.file_reference_context(run_workspace, attempt="attempt-1"):
        result = await ava.Agent(
            signature=Report, step_name="report", runtime=PydanticAIAgent(model=MODEL)
        )(question="why")

    schema = seen["infos"][0].output_tools[0].parameters_json_schema
    assert set(schema["properties"]) == {"report", "appendices"}
    report = result.outputs.report
    assert report.ref.path.startswith(".outputs/report/attempt-1/")
    assert report.ref.path.endswith("/report.md")
    assert report.read_bytes() == b"# Report"
    (appendix,) = result.outputs.appendices
    assert appendix.ref.path.endswith("/data/a.csv")


@pytest.mark.parametrize(
    ("report", "appendices", "problem"),
    [
        ({"name": "../report.md", "text": "x"}, [], "inside the output folder"),
        ({"name": "a.md", "text": "x"}, [{"name": "a.md", "text": "y"}], "more than once"),
    ],
)
@pytest.mark.asyncio
async def test_file_outputs_that_cannot_be_written_are_output_errors(
    script, tmp_path, report, appendices, problem
):
    script(_answer(report=report, appendices=appendices))
    run_workspace = ava.Workspace.from_directory(tmp_path, reference="snapshot:run")
    observed = []
    with ava.file_reference_context(run_workspace, attempt="attempt-1"):
        with capture_agent_evidence(observed.append, errors="raise"):
            with pytest.raises(AgentOutputError, match=problem):
                await ava.Agent(
                    signature=Report, step_name="report", runtime=PydanticAIAgent(model=MODEL)
                )(question="why")
    assert (observed[-1].kind, observed[-1].outputs) == ("trace_finished", None)


class Nested(ava.Signature):
    """Nest."""

    folder: dict[str, ava.File] = ava.InputField()
    answer: str = ava.OutputField()


@pytest.mark.asyncio
async def test_files_inside_other_types_are_rejected(script):
    script()
    with pytest.raises(AgentStepError, match="must be typed exactly"):
        await ava.Agent(
            signature=Nested, step_name="nested", runtime=PydanticAIAgent(model=MODEL)
        )(folder={})


class CodeWord(ava.Signature):
    """Give the code word of the project."""

    project: str = ava.InputField()
    code_word: str = ava.OutputField()


@pytest.mark.asyncio
async def test_skills_load_on_demand_and_their_files_are_readable(script, tmp_path):
    codes = tmp_path / "codes.md"
    codes.write_text("heron: PELICAN-4217")
    skill = ava.agent.Skill(
        name="project-codes",
        description="Looks up project code words.",
        instructions="Read references/codes.md and answer from it.",
        files={"references/codes.md": codes},
    )
    plain = ava.agent.Skill(name="greeting", description="Greets.", instructions="Say hi.")

    def after_load(messages, info):
        # The loaded skill's instructions reach the model only once it asked for them.
        assert "Read references/codes.md" in str(messages[-1])
        return _response(
            _call(
                "read_skill_file",
                {"skill": "project-codes", "path": "references/codes.md"},
                "read",
            ),
            _call("read_skill_file", {"skill": "greeting", "path": "x.md"}, "missing"),
        )

    seen = script(
        _response(_call("load_capability", {"id": "project-codes"}, "load")),
        after_load,
        _answer(code_word="PELICAN-4217"),
    )
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await ava.Agent(
            signature=CodeWord,
            step_name="code_word",
            runtime=PydanticAIAgent(model=MODEL),
            skills=(skill, plain),
        )(project="heron")

    assert result.outputs.code_word == "PELICAN-4217"
    first = seen["infos"][0]
    assert "project-codes: Looks up project code words." in first.instructions
    assert "Read references/codes.md" not in first.instructions
    assert [tool.name for tool in first.function_tools] == [
        "load_capability",
        "read_skill_file",
    ]
    turns = result.trace.turns
    assert [call.name for call in turns[0].tool_calls] == ["load_capability"]
    calls = {call.call_id: call for call in turns[1].tool_calls}
    assert calls["read"].result == "heron: PELICAN-4217"
    assert "has no file" in calls["missing"].error
    assert observed[1].native.skills == ("project-codes", "greeting")


@pytest.mark.asyncio
async def test_agent_options_reach_pydantic_ai_and_combine_with_avalanches(script):
    def double(value: int) -> int:
        """Double a value."""
        return value * 2

    extra = Capability(id="extra", instructions="Mind the extra capability.")
    seen = script(_answer(total=5))
    await _agent(
        retries=3,
        model_settings={"temperature": 0.1},
        tools=[double],
        capabilities=[extra],
    )(a=2, b=3)

    (options,) = seen["options"]
    assert (options["retries"], options["model_settings"]) == (3, {"temperature": 0.1})
    assert options["name"] == "total"
    assert options["capabilities"] == [extra]
    info = seen["infos"][0]
    assert [tool.name for tool in info.function_tools] == ["lookup_bonus", "double"]
    assert "Mind the extra capability." in info.instructions


@pytest.mark.parametrize(
    "option", ["output_type", "instructions", "system_prompt", "deps_type"]
)
def test_options_avalanche_fills_from_the_step_are_rejected(option):
    with pytest.raises(ValueError, match=f"does not take '{option}'"):
        PydanticAIAgent(model=MODEL, **{option: None})


def test_an_unknown_option_fails_when_the_runtime_is_built():
    with pytest.raises(ValueError, match=r"unknown options \['temprature'\]"):
        PydanticAIAgent(model=MODEL, temprature=0.2)


@pytest.mark.asyncio
async def test_a_combined_option_that_is_not_a_list_fails_the_step(script):
    script()
    with pytest.raises(AgentStepError, match="tools must be a list"):
        await _agent(tools=lookup_bonus)(a=2, b=3)


def test_declaration_and_merging_follow_the_runtime_rules():
    step = PydanticAIAgent(max_turns=3, retries=2)
    default = PydanticAIAgent(model=MODEL, model_settings={"temperature": 0})

    merged = step.merged_over(default)
    assert (merged.model, merged.max_turns) == (MODEL, 3)
    assert dict(merged.agent_options) == {"retries": 2, "model_settings": {"temperature": 0}}
    # Option values are never published; only their names are.
    assert step.declaration(workflow_default=default) == PydanticAIDeclaration(
        model=MODEL,
        settings={"max_turns": 3, "instructions_append": None},
        agent_options=("model_settings", "retries"),
    )


def test_a_step_declares_its_pydantic_ai_runtime():
    @ava.agent_step(Total, runtime=PydanticAIAgent(model=MODEL))
    async def total(a: int, b: int, *, agent: ava.Agent) -> int:
        return (await agent(a=a, b=b)).outputs.total

    declaration = total.fn.__agent_step__.declaration()
    assert declaration.runtime.kind == "pydantic_ai"
    assert declaration.runtime.model == MODEL


@pytest.mark.asyncio
async def test_without_pydantic_ai_the_step_says_which_extra_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "pydantic_ai", None)
    monkeypatch.delitem(sys.modules, "avalanche.runtimes.pydantic_ai._sdk")
    monkeypatch.delattr(importlib.import_module("avalanche.runtimes.pydantic_ai"), "_sdk")

    with pytest.raises(AgentStepExecutionError, match=r"avalanche-ai\[pydantic-ai\]"):
        await _agent()(a=2, b=3)


@pytest.mark.asyncio
async def test_large_tool_calls_stay_within_the_live_event_limits(script, monkeypatch):
    monkeypatch.setattr(sdk_module, "_MAX_VALUE_BYTES", 64)
    monkeypatch.setattr(sdk_module, "_MAX_TURN_TOOL_CALL_BYTES", 600)
    script(
        _response(*(_call("lookup_bonus", {"name": "x" * 100}, f"c{i}") for i in range(4))),
        _answer(total=5),
    )
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await _agent()(a=2, b=3)

    calls = result.trace.turns[0].tool_calls
    assert len(calls) == 4
    assert calls[0].arguments == {
        "kind": "unavailable",
        "reason": "value exceeds the trace size limit",
    }
    live = next(
        event.common
        for event in observed
        if event.kind == "event" and isinstance(event.common, TurnFinished)
    )
    assert live.omitted_tool_calls > 0
    assert live.tool_call_count == 4


_LIVE = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="set OPENAI_API_KEY to call OpenAI through Pydantic AI",
)
LIVE_MODEL = os.environ.get("AVALANCHE_LIVE_PYDANTIC_AI_MODEL", MODEL)


@_LIVE
@pytest.mark.asyncio
async def test_live_call_with_a_tool_and_a_small_model():
    class BonusTotal(ava.Signature):
        """Add a and b, plus the one bonus lookup_bonus returns for the name "team"."""

        a: int = ava.InputField()
        b: int = ava.InputField()
        total: int = ava.OutputField()

    observed = []
    agent = ava.Agent(
        signature=BonusTotal,
        step_name="total",
        runtime=PydanticAIAgent(model=LIVE_MODEL, max_turns=6),
        tools=(lookup_bonus,),
    )
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await agent(a=2, b=3)

    assert result.outputs.total == 15
    header = result.trace.header
    assert (header.runtime_kind, header.status) == ("pydantic_ai", "succeeded")
    assert header.usage is not None and header.usage.output_tokens > 0
    calls = [call.name for turn in result.trace.turns for call in turn.tool_calls]
    assert "lookup_bonus" in calls
    assert observed[-1].kind == "trace_finished"


@_LIVE
@pytest.mark.asyncio
async def test_live_reads_an_input_file_and_writes_a_file_output(tmp_path):
    class Shout(ava.Signature):
        """Write the attached document's text in upper case to a new text file."""

        document: ava.File = ava.InputField()
        shouted: ava.File = ava.OutputField()

    (tmp_path / "note.txt").write_text("quiet words")
    (tmp_path / "run").mkdir()
    run_workspace = ava.Workspace.from_directory(tmp_path / "run", reference="snapshot:run")
    agent = ava.Agent(
        signature=Shout, step_name="shout", runtime=PydanticAIAgent(model=LIVE_MODEL)
    )
    with ava.file_reference_context(run_workspace, attempt="attempt-1"):
        result = await agent(document=ava.File.from_path(tmp_path / "note.txt"))

    shouted = result.outputs.shouted
    assert shouted.ref.path.startswith(".outputs/shout/attempt-1/")
    assert shouted.read_bytes().decode().strip() == "QUIET WORDS"


@_LIVE
@pytest.mark.asyncio
async def test_live_edits_a_workspace_with_the_file_tools(tmp_path):
    class Rename(ava.Signature):
        """Read names.txt in the workspace and write the names sorted, one per line, to
        sorted.txt."""

        workspace: ava.Workspace = ava.InputField()
        edited: ava.Workspace = ava.OutputField()

    (tmp_path / "names.txt").write_text("carol\nalice\nbob\n")
    workspace = ava.Workspace.from_directory(tmp_path, reference="names")
    result = await ava.Agent(
        signature=Rename, step_name="rename", runtime=PydanticAIAgent(model=LIVE_MODEL)
    )(workspace=workspace)

    assert (tmp_path / "sorted.txt").read_text().split() == ["alice", "bob", "carol"]
    assert result.outputs.edited.path == tmp_path


@_LIVE
@pytest.mark.asyncio
async def test_live_loads_a_step_skill_and_reads_its_file(tmp_path):
    codes = tmp_path / "codes.md"
    codes.write_text("The code word of project heron is PELICAN-4217.\n")
    skill = ava.agent.Skill(
        name="project-codes",
        description="Looks up project code words. Use when asked for a project's code word.",
        instructions="Read references/codes.md of this skill and answer from it.",
        files={"references/codes.md": codes},
    )
    result = await ava.Agent(
        signature=CodeWord,
        step_name="code_word",
        runtime=PydanticAIAgent(model=LIVE_MODEL, max_turns=8),
        skills=(skill,),
    )(project="heron")

    assert result.outputs.code_word.strip() == "PELICAN-4217"
    calls = [call.name for turn in result.trace.turns for call in turn.tool_calls]
    assert {"load_capability", "read_skill_file"} <= set(calls)


@pytest.mark.asyncio
async def test_a_link_out_of_the_workspace_is_refused(script, tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    (folder / "link.txt").symlink_to(tmp_path / "secret.txt")
    script(
        _response(
            _call("read_file", {"path": "link.txt"}, "read"),
            _call("write_file", {"path": "link.txt", "text": "x"}, "write"),
        ),
        _answer(summary="done"),
    )
    result = await ava.Agent(
        signature=Edit, step_name="edit", runtime=PydanticAIAgent(model=MODEL)
    )(workspace=ava.Workspace.from_directory(folder, reference="work"))

    calls = result.trace.turns[0].tool_calls
    assert all("outside the working folder" in call.error for call in calls)
    assert (tmp_path / "secret.txt").read_text() == "secret"
