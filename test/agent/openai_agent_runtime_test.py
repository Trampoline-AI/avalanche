"""The OpenAI Agents SDK runtime, driven by the SDK's scripted test model."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib
import json
import os
import sys
from collections.abc import Awaitable
from pathlib import Path

import pytest
from pydantic import BaseModel

pytest.importorskip("agents")

from agents import MaxTurnsExceeded, Usage  # noqa: E402
from agents.models.interface import ModelProvider  # noqa: E402
from agents.testing import (  # noqa: E402
    ModelStep,
    ScriptedModel,
    assistant_message,
    function_call,
)
from openai.types.responses import (  # noqa: E402
    Response,
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseCustomToolCall,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseReasoningItem,
)
from openai.types.responses.response_reasoning_item import Summary  # noqa: E402
from openai.types.responses.response_usage import (  # noqa: E402
    InputTokensDetails,
    OutputTokensDetails,
)
from openai.types.shared import Reasoning  # noqa: E402

import avalanche as ava  # noqa: E402
from avalanche._agent_evidence import capture_agent_evidence  # noqa: E402
from avalanche.agent import (  # noqa: E402
    AgentOutputError,
    AgentStepError,
    AgentStepExecutionError,
)
from avalanche.agent.declaration import OpenAIAgentDeclaration  # noqa: E402
from avalanche.agent.live import (  # noqa: E402
    AgentLiveEvent,
    AgentTraceFinished,
    AgentTraceUnavailable,
    RunStarted,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from avalanche.agent.native.openai_agent import AgentStarted  # noqa: E402
from avalanche.agent.trace import TokenUsage  # noqa: E402
from avalanche.runtimes import OpenAIAgent  # noqa: E402

sdk_module = importlib.import_module("avalanche.runtimes.openai_agent._sdk")

MODEL = "gpt-5.4-mini"
SUMMARY = "Looked up the bonus and added it."


class Total(ava.Signature):
    """Add the numbers and the bonus from lookup_bonus."""

    a: int = ava.InputField()
    b: int = ava.InputField()
    total: int = ava.OutputField()


def lookup_bonus(name: str) -> int:
    """Return the bonus for a name."""
    if name == "nobody":
        raise LookupError("no bonus for nobody")
    return 10


def _usage(*, input_tokens=100, cached=60, written=10, output=20, reasoning=5) -> Usage:
    return Usage(
        requests=1,
        input_tokens=input_tokens,
        input_tokens_details=InputTokensDetails(
            cached_tokens=cached, cache_write_tokens=written
        ),
        output_tokens=output,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=reasoning),
        total_tokens=input_tokens + output,
    )


def _answer(response_id="resp-answer", summary: str | None = SUMMARY, **fields) -> ModelStep:
    """The model's final response: the step's answer as JSON."""
    if summary is not None:
        fields["run_summary"] = summary
    return ModelStep(
        output=[assistant_message(json.dumps(fields), item_id=f"msg-{response_id}")],
        usage=_usage(),
        response_id=response_id,
    )


def _lookup(name="a", *, call_id="call-1") -> ModelStep:
    return ModelStep(
        output=[
            ResponseReasoningItem(
                id="reasoning-1",
                type="reasoning",
                summary=[Summary(text="I need the bonus.", type="summary_text")],
            ),
            assistant_message("Looking up the bonus.", item_id="msg-lookup"),
            function_call("lookup_bonus", {"name": name}, call_id=call_id),
        ],
        usage=_usage(),
        response_id="resp-lookup",
    )


@pytest.fixture
def scripted(monkeypatch):
    """Answer the agent's model calls from a script; returns the scripted model.

    ``scripted.names`` collects the model names the SDK asked the provider for.
    """
    names: list[str | None] = []

    def install(*steps: ModelStep) -> ScriptedModel:
        model = ScriptedModel(list(steps))

        class Scripted(ModelProvider):
            def get_model(self, model_name: str | None) -> ScriptedModel:
                names.append(model_name)
                return model

        @contextlib.asynccontextmanager
        async def models():
            yield Scripted()

        monkeypatch.setattr(sdk_module, "_models", models)
        return model

    install.names = names
    return install


def _agent(signature=Total, tools=(lookup_bonus,), skills=(), **runtime) -> ava.Agent:
    return ava.Agent(
        signature=signature,
        step_name="total",
        runtime=OpenAIAgent(model=MODEL, **runtime),
        tools=tools,
        skills=skills,
    )


def _live(observed) -> list[AgentLiveEvent]:
    return [event for event in observed if isinstance(event, AgentLiveEvent)]


async def _observed(agent: ava.Agent, **inputs):
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await agent(**inputs)
    return result, observed


@pytest.mark.asyncio
async def test_a_run_maps_onto_outputs_turns_and_live_events(scripted):
    scripted(_lookup(), _answer(total=15))
    result, observed = await _observed(_agent(max_turns=6), a=2, b=3)

    assert result.outputs.total == 15
    header = result.trace.header
    assert (header.runtime_kind, header.status, header.stop_reason, header.model) == (
        "openai_agent",
        "succeeded",
        "final_output",
        "scripted-model",
    )
    assert (header.turn_count, header.tool_call_count) == (2, 1)
    # OpenAI counts cached and cache-written input in input_tokens; the trace splits them.
    assert header.usage == TokenUsage(
        input_tokens=60,
        output_tokens=40,
        cache_read_input_tokens=120,
        cache_creation_input_tokens=20,
    )
    assert (header.native.max_turns, header.native.requests) == (6, 2)
    assert (header.native.reasoning_tokens, header.native.last_response_id) == (
        10,
        "resp-answer",
    )
    assert header.external_run_id is None

    lookup, answer = result.trace.turns
    assert (lookup.message, lookup.reasoning) == ("Looking up the bonus.", "I need the bonus.")
    (call,) = lookup.tool_calls
    assert (call.call_id, call.name, call.arguments, call.result, call.error) == (
        "call-1",
        "lookup_bonus",
        {"name": "a"},
        "10",
        None,
    )
    assert call.duration_ms is not None and lookup.duration_ms is not None
    assert lookup.usage == TokenUsage(
        input_tokens=30,
        output_tokens=20,
        cache_read_input_tokens=60,
        cache_creation_input_tokens=10,
    )
    assert (lookup.native.response_id, lookup.native.answer, lookup.failed) == (
        "resp-lookup",
        False,
        False,
    )
    # The answering turn shows the run summary, not the JSON answer the outputs hold.
    assert (answer.message, answer.tool_calls, answer.native.answer) == (SUMMARY, (), True)

    kinds = [
        type(event.common or event.native).__name__
        for event in observed
        if not isinstance(event, AgentTraceFinished)
    ]
    assert kinds == [
        "RunStarted",
        "AgentStarted",
        "ToolStarted",
        "ToolFinished",
        "TurnFinished",
        "TurnFinished",
    ]
    started = observed[1].native
    assert started == AgentStarted(
        agent_name="total", model=MODEL, tools=("lookup_bonus",), builtin_tools=()
    )
    assert [
        e.turn for e in _live(observed) if isinstance(e.common, ToolStarted | ToolFinished)
    ] == [
        1,
        1,
    ]
    final = observed[-1]
    assert isinstance(final, AgentTraceFinished)
    assert (final.header, final.outputs) == (header, {"total": 15})


@pytest.mark.asyncio
async def test_the_agent_gets_the_instructions_inputs_answer_schema_and_settings(scripted):
    model = scripted(_answer(total=5))
    await _agent(system_prompt_append="Be brief.", temperature=0.25)(a=2, b=3)

    call = model.first_call
    instructions = call.system_instructions
    assert instructions.startswith("Add the numbers and the bonus from lookup_bonus.")
    assert "run_summary" in instructions and instructions.endswith("Be brief.")
    (message,) = call.input
    (prompt,) = message["content"]
    assert json.loads(prompt["text"].split("\n\n", 1)[1]) == {"a": 2, "b": 3}
    schema = call.output_schema.json_schema()
    assert set(schema["required"]) == {"total", "run_summary"}
    assert call.output_schema.is_strict_json_schema()
    assert call.model_settings.temperature == 0.25
    assert [tool.name for tool in call.tools] == ["lookup_bonus"]
    assert scripted.names == [MODEL]


@pytest.mark.asyncio
async def test_without_a_model_the_sdk_default_answers(scripted):
    scripted(_answer(total=5))
    result, observed = await _observed(
        ava.Agent(signature=Total, step_name="total", runtime=OpenAIAgent()), a=2, b=3
    )

    # The SDK's OpenAI provider resolves no name to its default model.
    assert scripted.names == [None]
    assert observed[0].common.model is None
    # The header names the model that answered.
    assert result.trace.header.model == "scripted-model"


@pytest.mark.asyncio
async def test_without_narration_the_answering_turn_shows_the_answer_it_wrote(scripted):
    model = scripted(_answer(summary=None, total=5))
    result = await _agent(narrate=False)(a=2, b=3)

    assert result.outputs.total == 5
    assert "run_summary" not in model.first_call.output_schema.json_schema()["properties"]
    assert "run_summary" not in model.first_call.system_instructions
    assert json.loads(result.trace.turns[0].message) == {"total": 5}


@pytest.mark.asyncio
async def test_a_signature_output_named_run_summary_needs_narration_off(scripted):
    class Report(ava.Signature):
        """Report."""

        text: str = ava.InputField()
        run_summary: str = ava.OutputField()

    scripted(_answer(summary=None, run_summary="mine"))
    with pytest.raises(AgentStepError, match="narrate=False"):
        await ava.Agent(signature=Report, step_name="report", runtime=OpenAIAgent())(text="x")
    result = await ava.Agent(
        signature=Report, step_name="report", runtime=OpenAIAgent(narrate=False)
    )(text="x")
    assert result.outputs.run_summary == "mine"


@pytest.mark.asyncio
async def test_a_failed_tool_reaches_the_model_and_is_kept_on_its_call(scripted):
    model = scripted(_lookup("nobody"), _answer(total=5))
    result, observed = await _observed(_agent(), a=2, b=3)

    (call,) = result.trace.turns[0].tool_calls
    assert (call.result, call.error) == (None, "LookupError: no bonus for nobody")
    finished = next(e.common for e in _live(observed) if isinstance(e.common, ToolFinished))
    assert finished.error == "LookupError: no bonus for nobody"
    assert result.trace.turns[0].failed is False
    # The model got the error as the tool's result and could go on.
    (output,) = [
        item for item in model.last_call.input if item.get("type") == "function_call_output"
    ]
    assert output["output"] == "LookupError: no bonus for nobody"


@pytest.mark.asyncio
async def test_a_tool_result_reaches_the_model_as_json(scripted):
    class Lookup(ava.Signature):
        """Look the name up."""

        name: str = ava.InputField()
        found: str = ava.OutputField()

    async def profile(name: str) -> dict[str, int]:
        """Return the profile of a name."""
        return {"age": 3}

    model = scripted(
        ModelStep(output=[function_call("profile", {"name": "a"}, call_id="call-1")]),
        _answer(found="yes"),
    )
    result = await ava.Agent(
        signature=Lookup, step_name="lookup", runtime=OpenAIAgent(), tools=(profile,)
    )(name="a")

    assert result.trace.turns[0].tool_calls[0].result == '{"age":3}'
    (output,) = [
        item for item in model.last_call.input if item.get("type") == "function_call_output"
    ]
    assert json.loads(output["output"]) == {"age": 3}


@pytest.mark.asyncio
async def test_a_tool_that_returns_an_awaitable_is_awaited(scripted):
    async def bonus(name: str) -> int:
        return 10

    def lookup_bonus(name: str) -> Awaitable[int]:
        """Return the bonus for a name."""
        return bonus(name)

    scripted(_lookup(), _answer(total=15))
    result = await _agent(tools=(lookup_bonus,))(a=2, b=3)

    assert result.trace.turns[0].tool_calls[0].result == "10"


def _waiting_tool() -> tuple[object, dict[str, bool]]:
    """A step tool that waits until it is cancelled, and whether it is still running."""
    state = {"started": False, "running": False}

    async def lookup_bonus(name: str) -> int:
        """Return the bonus for a name."""
        state["started"] = state["running"] = True
        try:
            await asyncio.Event().wait()
        finally:
            state["running"] = False
        return 0

    return lookup_bonus, state


@pytest.mark.asyncio
async def test_a_failing_observer_stops_the_run_before_the_call_ends(scripted):
    tool, state = _waiting_tool()
    scripted(_lookup(), _answer(total=5))

    def observe(event):
        if isinstance(event, AgentLiveEvent) and isinstance(event.common, ToolStarted):
            raise RuntimeError("observer failed")

    with (
        capture_agent_evidence(observe, errors="raise"),
        pytest.raises(AgentStepExecutionError, match="observer failed"),
    ):
        await _agent(tools=(tool,))(a=2, b=3)

    started = state["started"]
    assert not state["running"]
    # Nothing of the run is left to start the tool later.
    await asyncio.sleep(0.05)
    assert state == {"started": started, "running": False}


@pytest.mark.asyncio
async def test_an_observer_that_keeps_failing_still_gets_the_final_event(scripted):
    tool, _ = _waiting_tool()
    scripted(_lookup(), _answer(total=5))
    observed = []

    def observe(event):
        observed.append(event)
        if isinstance(event, AgentLiveEvent) and isinstance(
            event.common, ToolStarted | TurnFinished
        ):
            raise RuntimeError(f"observer failed on {event.common.kind}")

    with (
        capture_agent_evidence(observe, errors="raise"),
        pytest.raises(RuntimeError, match="turn.finished") as raised,
    ):
        await _agent(tools=(tool,))(a=2, b=3)

    # The first failure is kept as the context of the second.
    assert "tool.started" in str(raised.value.__context__)
    assert isinstance(observed[-1], AgentTraceFinished)
    assert observed[-1].header.status == "failed"


@pytest.mark.asyncio
async def test_an_observer_failing_on_the_answering_turn_still_gets_the_final_event(scripted):
    scripted(_answer(total=5))
    observed = []

    def observe(event):
        observed.append(event)
        if isinstance(event, AgentLiveEvent) and isinstance(event.common, TurnFinished):
            raise RuntimeError("observer failed")

    with (
        capture_agent_evidence(observe, errors="raise"),
        pytest.raises(RuntimeError, match="observer failed"),
    ):
        await _agent()(a=2, b=3)

    assert isinstance(observed[-1], AgentTraceFinished)
    assert observed[-1].outputs is None


@pytest.mark.asyncio
async def test_a_cancelled_call_stops_the_tool_it_was_running(scripted):
    tool, state = _waiting_tool()
    scripted(_lookup(), _answer(total=5))
    task = asyncio.create_task(_agent(tools=(tool,))(a=2, b=3))
    while not state["started"]:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not state["running"]


@pytest.mark.asyncio
async def test_with_narration_an_answer_without_its_summary_fails_the_run(scripted):
    scripted(_answer(summary=None, total=5))
    observed = []
    with (
        capture_agent_evidence(observed.append, errors="raise"),
        pytest.raises(AgentStepExecutionError, match="run_summary"),
    ):
        await _agent()(a=2, b=3)

    final = observed[-1]
    assert isinstance(final, AgentTraceFinished)
    assert (final.header.status, final.outputs) == ("failed", None)


@pytest.mark.asyncio
async def test_a_refusal_fails_its_turn(scripted):
    refusal = ResponseOutputMessage(
        id="msg-refusal",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputRefusal(refusal="I can't help with that.", type="refusal")],
    )
    scripted(
        ModelStep(
            output=[refusal, function_call("lookup_bonus", {"name": "a"}, call_id="call-1")]
        ),
        _answer(total=15),
    )
    result, observed = await _observed(_agent(), a=2, b=3)

    turn = result.trace.turns[0]
    assert (turn.native.refusal, turn.failed) == ("I can't help with that.", True)
    assert next(e for e in _live(observed) if isinstance(e.common, TurnFinished)).failed


@pytest.mark.asyncio
async def test_a_run_over_its_turn_limit_fails_with_the_turns_it_took(scripted):
    scripted(_lookup(call_id="call-1"), _lookup(call_id="call-2"))
    observed = []
    with (
        capture_agent_evidence(observed.append, errors="raise"),
        pytest.raises(AgentStepExecutionError, match="MaxTurnsExceeded") as raised,
    ):
        await _agent(max_turns=2)(a=2, b=3)

    assert isinstance(raised.value.__cause__, MaxTurnsExceeded)
    final = observed[-1]
    assert isinstance(final, AgentTraceFinished)
    assert (final.header.status, final.header.stop_reason, final.outputs) == (
        "failed",
        "max_turns_exceeded",
        None,
    )
    assert (final.header.turn_count, final.header.tool_call_count) == (2, 2)
    assert final.header.native.errors[0].startswith("MaxTurnsExceeded: ")
    assert len([e for e in _live(observed) if isinstance(e.common, TurnFinished)]) == 2


@pytest.mark.asyncio
async def test_a_model_error_fails_the_run_with_its_reason(scripted):
    class QuotaError(Exception):
        pass

    scripted(_lookup(), ModelStep(error=QuotaError("insufficient_quota")))
    observed = []
    with (
        capture_agent_evidence(observed.append, errors="raise"),
        pytest.raises(AgentStepExecutionError, match="insufficient_quota"),
    ):
        await _agent()(a=2, b=3)

    final = observed[-1]
    assert (final.header.stop_reason, final.header.native.errors) == (
        "quota_error",
        ("QuotaError: insufficient_quota",),
    )
    # The turn before the failed request keeps its tool call and result.
    assert final.header.turn_count == 1
    (call,) = next(
        e for e in _live(observed) if isinstance(e.common, TurnFinished)
    ).common.turn.tool_calls
    assert call.result == "10"


@pytest.mark.asyncio
async def test_a_cancelled_call_reports_no_trace(scripted):
    async def never(call):
        await asyncio.Event().wait()
        yield

    scripted(ModelStep.stream(never))
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        task = asyncio.create_task(_agent()(a=2, b=3))
        # Wait for the agent to start: run.started, then agent.started.
        while len(observed) < 2:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    final = observed[-1]
    assert isinstance(final, AgentTraceUnavailable)
    assert (final.status, final.runtime_kind) == ("cancelled", "openai_agent")


@pytest.fixture
def recorded_traces():
    """The SDK traces of the test, kept here instead of being sent to OpenAI."""
    from agents.tracing import TracingProcessor, set_trace_processors
    from agents.tracing.processors import default_processor

    traces: list[str] = []

    class Recording(TracingProcessor):
        def on_trace_start(self, trace):
            traces.append(trace.trace_id)

        def on_trace_end(self, trace):
            pass

        def on_span_start(self, span):
            pass

        def on_span_end(self, span):
            pass

        def shutdown(self):
            pass

        def force_flush(self):
            pass

    set_trace_processors([Recording()])
    yield traces
    set_trace_processors([default_processor()])


@pytest.mark.asyncio
async def test_sdk_tracing_is_off_unless_asked_and_names_the_external_run(
    scripted, recorded_traces
):
    scripted(_answer(total=5), _answer(total=5))
    untraced = await _agent()(a=2, b=3)
    assert recorded_traces == [] and untraced.trace.header.external_run_id is None

    traced = await _agent(tracing=True)(a=2, b=3)
    assert recorded_traces == [traced.trace.header.external_run_id]


@pytest.mark.asyncio
async def test_file_inputs_are_attached_to_the_prompt(scripted, tmp_path):
    class Describe(ava.Signature):
        """Describe the files."""

        document: ava.File = ava.InputField()
        pictures: list[ava.File] = ava.InputField()
        description: str = ava.OutputField()

    (tmp_path / "note.txt").write_text("quiet words")
    (tmp_path / "dot.png").write_bytes(b"\x89PNG fake")
    model = scripted(_answer(description="a note and a picture"))
    result, observed = await _observed(
        ava.Agent(signature=Describe, step_name="describe", runtime=OpenAIAgent()),
        document=ava.File.from_path(tmp_path / "note.txt"),
        pictures=[ava.File.from_path(tmp_path / "dot.png")],
    )

    assert result.outputs.description == "a note and a picture"
    (message,) = model.first_call.input
    prompt, first_label, note, second_label, picture = message["content"]
    assert json.loads(prompt["text"].split("\n\n", 1)[1]) == {
        "document": "attachment 1: note.txt",
        "pictures": ["attachment 2: dot.png"],
    }
    assert (first_label["text"], second_label["text"]) == (
        "Attachment 1: note.txt",
        "Attachment 2: dot.png",
    )
    assert (note["type"], note["filename"]) == ("input_file", "note.txt")
    assert (
        note["file_data"]
        == "data:text/plain;base64," + base64.b64encode(b"quiet words").decode()
    )
    assert picture["type"] == "input_image"
    assert picture["image_url"].startswith("data:image/png;base64,")
    # The trace names the files by reference, never their bytes.
    started = observed[0].common
    assert isinstance(started, RunStarted)
    assert started.inputs["document"]["kind"] == "file"


class Shout(ava.Signature):
    """Write the text in upper case to a file."""

    text: str = ava.InputField()
    shouted: ava.File = ava.OutputField()
    extras: list[ava.File] = ava.OutputField()


@pytest.mark.asyncio
async def test_file_outputs_are_written_in_the_run_workspace(scripted, tmp_path):
    model = scripted(
        _answer(
            shouted={"name": "shout.txt", "content": "QUIET WORDS"},
            extras=[{"name": "a.md", "content": "# A"}],
        )
    )
    (tmp_path / "run").mkdir()
    workspace = ava.Workspace.from_directory(tmp_path / "run", reference="snapshot:run")
    observed = []
    with (
        capture_agent_evidence(observed.append, errors="raise"),
        ava.file_reference_context(workspace, attempt="attempt-1"),
    ):
        result = await ava.Agent(signature=Shout, step_name="shout", runtime=OpenAIAgent())(
            text="quiet words"
        )

    shouted = result.outputs.shouted
    assert shouted.ref.path.startswith(".outputs/shout/attempt-1/")
    assert shouted.read_bytes() == b"QUIET WORDS"
    (extra,) = result.outputs.extras
    assert extra.read_bytes() == b"# A"
    schema = model.first_call.output_schema.json_schema()
    assert schema["properties"]["shouted"]["required"] == ["name", "content"]
    assert observed[-1].outputs["shouted"]["kind"] == "file"
    assert "whole text" in model.first_call.input[0]["content"][0]["text"]


@pytest.mark.parametrize("name", ["../escape.txt", "folder/file.txt", ".."])
@pytest.mark.asyncio
async def test_a_file_output_must_be_a_plain_file_name(scripted, tmp_path, name):
    scripted(_answer(shouted={"name": name, "content": "x"}, extras=[]))
    (tmp_path / "run").mkdir()
    workspace = ava.Workspace.from_directory(tmp_path / "run", reference="snapshot:run")
    observed = []
    with (
        capture_agent_evidence(observed.append, errors="raise"),
        ava.file_reference_context(workspace, attempt="attempt-1"),
        pytest.raises(AgentOutputError, match="'shouted' is not a usable file"),
    ):
        await ava.Agent(signature=Shout, step_name="shout", runtime=OpenAIAgent())(text="x")

    assert (observed[-1].header.status, observed[-1].outputs) == ("succeeded", None)
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.asyncio
async def test_an_answer_without_a_file_output_fails_after_the_final_event(scripted, tmp_path):
    scripted(_answer(extras=[]))
    (tmp_path / "run").mkdir()
    workspace = ava.Workspace.from_directory(tmp_path / "run", reference="snapshot:run")
    observed = []
    with (
        capture_agent_evidence(observed.append, errors="raise"),
        ava.file_reference_context(workspace, attempt="attempt-1"),
        pytest.raises(AgentOutputError, match="'shouted' is not a usable file"),
    ):
        await ava.Agent(signature=Shout, step_name="shout", runtime=OpenAIAgent())(text="x")

    assert observed[-1].outputs is None


@pytest.mark.asyncio
async def test_an_answer_that_does_not_match_the_signature_fails_after_the_final_event(
    scripted,
):
    scripted(_answer(total="15"))
    observed = []
    with (
        capture_agent_evidence(observed.append, errors="raise"),
        pytest.raises(AgentOutputError),
    ):
        await _agent()(a=2, b=3)
    assert observed[-1].outputs == {"total": "15"}


class Attachment(BaseModel):
    file: ava.File


class WorkspaceInput(ava.Signature):
    folder: ava.Workspace = ava.InputField()
    answer: str = ava.OutputField()


class TwoWorkspaces(ava.Signature):
    first: ava.Workspace = ava.InputField()
    second: ava.Workspace = ava.InputField()
    answer: str = ava.OutputField()


class WorkspaceInModelInput(ava.Signature):
    folders: list[ava.Workspace] = ava.InputField()
    answer: str = ava.OutputField()


class WorkspaceOutput(ava.Signature):
    question: str = ava.InputField()
    folder: ava.Workspace = ava.OutputField()


class FileInModelInput(ava.Signature):
    attachment: Attachment = ava.InputField()
    answer: str = ava.OutputField()


class OptionalFileOutput(ava.Signature):
    question: str = ava.InputField()
    report: ava.File | None = ava.OutputField()


@pytest.mark.parametrize(
    ("signature", "inputs", "builtin_tools", "match"),
    [
        (WorkspaceInput, {"folder": ava.Workspace()}, (), "sandbox tools"),
        (WorkspaceOutput, {"question": "q"}, (), "sandbox tools"),
        (
            TwoWorkspaces,
            {"first": ava.Workspace(), "second": ava.Workspace()},
            ("shell",),
            "at most one",
        ),
        (WorkspaceInModelInput, {"folders": []}, ("shell",), "typed exactly"),
        (
            FileInModelInput,
            {"attachment": Attachment(file=ava.File.from_path(Path(__file__)))},
            (),
            "typed exactly",
        ),
        (OptionalFileOutput, {"question": "q"}, (), "typed exactly"),
    ],
    ids=[
        "workspace-input-without-sandbox",
        "workspace-output-without-sandbox",
        "two-workspaces",
        "workspace-in-model-input",
        "file-in-model-input",
        "optional-file-output",
    ],
)
@pytest.mark.asyncio
async def test_unsupported_file_fields_are_rejected_before_the_call(
    scripted, signature, inputs, builtin_tools, match
):
    model = scripted()
    observed = []
    with (
        capture_agent_evidence(observed.append, errors="raise"),
        pytest.raises(AgentStepError, match=match),
    ):
        await ava.Agent(
            signature=signature,
            step_name="step",
            runtime=OpenAIAgent(builtin_tools=builtin_tools),
        )(**inputs)
    assert observed == [] and model.calls == ()


@pytest.mark.asyncio
async def test_outputs_strict_json_cannot_express_are_rejected_before_the_call(scripted):
    class Counts(ava.Signature):
        """Count the words."""

        text: str = ava.InputField()
        counts: dict[str, int] = ava.OutputField()

    model = scripted()
    with pytest.raises(AgentStepError, match="strict JSON schemas"):
        await ava.Agent(signature=Counts, step_name="counts", runtime=OpenAIAgent())(text="x")
    assert model.calls == ()


def _skill(tmp_path: Path) -> ava.agent.Skill:
    codes = tmp_path / "codes.md"
    codes.write_text("The code word of project heron is PELICAN-4217.\n")
    return ava.agent.Skill(
        name="project-codes",
        description="Looks up project code words.",
        instructions="Read references/codes.md and answer from it.",
        files={"references/codes.md": codes},
    )


class CodeWord(ava.Signature):
    """Give the code word of the project."""

    project: str = ava.InputField()
    code_word: str = ava.OutputField()


@pytest.mark.asyncio
async def test_skills_are_listed_in_the_instructions_and_read_through_tools(scripted, tmp_path):
    model = scripted(
        ModelStep(
            output=[function_call("load_skill", {"name": "project-codes"}, call_id="call-1")]
        ),
        ModelStep(
            output=[
                function_call(
                    "read_skill_file",
                    {"skill": "project-codes", "path": "references/codes.md"},
                    call_id="call-2",
                ),
                function_call(
                    "read_skill_file",
                    {"skill": "project-codes", "path": "scripts/run.py"},
                    call_id="call-3",
                ),
            ]
        ),
        _answer(code_word="PELICAN-4217"),
    )
    result = await ava.Agent(
        signature=CodeWord,
        step_name="code_word",
        runtime=OpenAIAgent(),
        skills=(_skill(tmp_path),),
    )(project="heron")

    assert result.outputs.code_word == "PELICAN-4217"
    instructions = model.first_call.system_instructions
    assert "- project-codes: Looks up project code words." in instructions
    assert [tool.name for tool in model.first_call.tools] == ["load_skill", "read_skill_file"]
    (load,) = result.trace.turns[0].tool_calls
    assert "Read references/codes.md and answer from it." in load.result
    assert "- references/codes.md" in load.result
    read, missing = result.trace.turns[1].tool_calls
    assert read.result == "The code word of project heron is PELICAN-4217.\n"
    assert missing.error.startswith("ValueError: skill 'project-codes' has no file")


@pytest.mark.asyncio
async def test_skill_tool_names_are_reserved_when_the_step_has_skills(scripted, tmp_path):
    def load_skill(name: str) -> str:
        """Mine."""
        return name

    scripted()
    with pytest.raises(AgentStepError, match="reserved"):
        await ava.Agent(
            signature=CodeWord,
            step_name="code_word",
            runtime=OpenAIAgent(),
            tools=(load_skill,),
            skills=(_skill(tmp_path),),
        )(project="heron")


def _patch(item_id: str, patch: str, *, call_id: str) -> ModelStep:
    """A response calling apply_patch, a custom tool the SDK streams only as given."""
    call = ResponseCustomToolCall(
        type="custom_tool_call", id=item_id, call_id=call_id, name="apply_patch", input=patch
    )
    done = Response(
        id=f"resp-{call_id}",
        created_at=0,
        model=MODEL,
        object="response",
        output=[call],
        tool_choice="auto",
        tools=[],
        top_p=None,
        parallel_tool_calls=False,
        status="completed",
    )
    started = done.model_copy(update={"output": [], "status": "in_progress"})
    return ModelStep.stream(
        [
            ResponseCreatedEvent(type="response.created", response=started, sequence_number=0),
            ResponseCompletedEvent(type="response.completed", response=done, sequence_number=1),
        ],
        output=[call],
        response_id=done.id,
    )


def _shell(command: str, *, call_id: str) -> ModelStep:
    return ModelStep(output=[function_call("exec_command", {"cmd": command}, call_id=call_id)])


class Edit(ava.Signature):
    """Write notes.txt into the workspace."""

    workspace: ava.Workspace = ava.InputField()
    document: ava.File = ava.InputField()
    edited: ava.Workspace = ava.OutputField()
    note: str = ava.OutputField()


@pytest.mark.asyncio
async def test_the_sandbox_edits_a_workspace_in_place_and_returns_it(
    scripted, tmp_path, monkeypatch
):
    # The SDK would keep its own snapshot of the workspace under the state folder.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "existing.txt").write_text("kept")
    (folder / "draft.txt").write_text("draft")
    workspace = ava.Workspace.from_directory(folder, reference="notes")
    patch = "*** Begin Patch\n*** Add File: notes.txt\n+written by the agent\n*** End Patch\n"
    model = scripted(
        _shell("cat existing.txt", call_id="call-1"),
        _patch("patch-1", patch, call_id="call-2"),
        _answer(note="done"),
    )
    observed = []
    with capture_agent_evidence(observed.append, errors="raise"):
        result = await ava.Agent(
            signature=Edit,
            step_name="edit",
            runtime=OpenAIAgent(model=MODEL, builtin_tools=["filesystem", "shell"]),
        )(workspace=workspace, document=ava.File.from_path(folder / "draft.txt"))

    assert (folder / "notes.txt").read_text() == "written by the agent"
    assert sorted(path.name for path in folder.iterdir()) == [
        "draft.txt",
        "existing.txt",
        "notes.txt",
    ]
    assert result.outputs.note == "done"
    assert result.outputs.edited.reference == "notes"
    # The returned reference stays bound to the directory the agent worked in.
    assert result.outputs.edited.path == folder
    assert not (tmp_path / "state").exists()

    call = model.first_call
    assert {"apply_patch", "view_image", "exec_command"} <= {tool.name for tool in call.tools}
    assert "edited" not in call.output_schema.json_schema()["properties"]
    prompt = json.loads(call.input[0]["content"][0]["text"].split("\n\n", 1)[1])
    # A file inside the sandbox is named by its path there, and still attached.
    assert prompt["document"] == "attachment 1: draft.txt, at draft.txt"
    assert prompt["workspace"].startswith("the sandbox workspace")

    shell, edit = (turn.tool_calls[0] for turn in result.trace.turns[:2])
    assert (shell.name, shell.arguments) == ("exec_command", {"cmd": "cat existing.txt"})
    assert "kept" in shell.result
    assert (edit.name, edit.arguments, edit.result) == (
        "apply_patch",
        patch,
        "Created notes.txt",
    )
    started = next(event.native for event in observed if isinstance(event.native, AgentStarted))
    assert started.builtin_tools == ("filesystem", "shell")


class Report(ava.Signature):
    question: str = ava.InputField()
    report: ava.File = ava.OutputField()
    appendices: list[ava.File] = ava.OutputField()


@pytest.mark.asyncio
async def test_a_workspace_with_a_link_fails_after_the_final_event(scripted):
    # Without a workspace input the sandbox is a temporary folder, captured when returned.
    scripted(_shell("touch a.txt && ln -s a.txt link.txt", call_id="call-1"), _answer())
    observed = []
    with (
        capture_agent_evidence(observed.append, errors="raise"),
        pytest.raises(AgentOutputError, match="workspace cannot be returned"),
    ):
        await ava.Agent(
            signature=WorkspaceOutput,
            step_name="folder",
            runtime=OpenAIAgent(model=MODEL, builtin_tools=["shell"]),
        )(question="?")

    assert observed[-1].outputs is None


@pytest.mark.asyncio
async def test_sandbox_file_outputs_are_files_the_model_writes(scripted, tmp_path):
    seen: dict[str, str] = {}

    def write_files(call) -> ModelStep:
        prompt = call.input[0]["content"][0]["text"]
        seen["folder"] = prompt.split("output inside ", 1)[1].split(" and give", 1)[0]
        command = (
            f"printf report > {seen['folder']}/report.bin; printf a > {seen['folder']}/a.txt"
        )
        return _shell(command, call_id="call-1")

    def answer(call) -> ModelStep:
        return _answer(
            report=f"{seen['folder']}/report.bin", appendices=[f"{seen['folder']}/a.txt"]
        )

    model = scripted(ModelStep.respond(write_files), ModelStep.respond(answer))
    (tmp_path / "run").mkdir()
    workspace = ava.Workspace.from_directory(tmp_path / "run", reference="snapshot:run")
    with ava.file_reference_context(workspace, attempt="attempt-1"):
        result = await ava.Agent(
            signature=Report,
            step_name="report",
            runtime=OpenAIAgent(builtin_tools=["shell"]),
        )(question="q")

    assert result.outputs.report.ref.path.startswith(".outputs/report/attempt-1/")
    assert result.outputs.report.read_bytes() == b"report"
    assert [file.read_bytes() for file in result.outputs.appendices] == [b"a"]
    schema = model.first_call.output_schema.json_schema()["properties"]
    assert schema["report"]["type"] == "string"


@pytest.mark.asyncio
async def test_with_a_shell_the_model_runs_a_skill_script(scripted, tmp_path):
    script = tmp_path / "code.sh"
    script.write_text("#!/bin/sh\necho PELICAN-4217\n")
    script.chmod(0o755)
    skill = ava.agent.Skill(
        name="project-codes",
        description="Looks up project code words.",
        instructions="Run scripts/code.sh and answer with what it prints.",
        files={"scripts/code.sh": script},
    )

    def run_script(call) -> ModelStep:
        folder = call.system_instructions.split("files are also in ", 1)[1].split("/<skill", 1)[
            0
        ]
        return _shell(f"{folder}/project-codes/scripts/code.sh", call_id="call-1")

    model = scripted(ModelStep.respond(run_script), _answer(code_word="PELICAN-4217"))
    result = await ava.Agent(
        signature=CodeWord,
        step_name="code_word",
        runtime=OpenAIAgent(builtin_tools=["shell"]),
        skills=(skill,),
    )(project="heron")

    assert result.outputs.code_word == "PELICAN-4217"
    (run,) = result.trace.turns[0].tool_calls
    assert "PELICAN-4217" in run.result
    assert "load_skill" in {tool.name for tool in model.first_call.tools}
    # The skills' folder is made for the call and removed after it.
    folder = Path(run.arguments["cmd"].split("/project-codes/", 1)[0])
    assert not folder.exists()


@pytest.mark.asyncio
async def test_sandbox_tool_names_are_reserved_when_the_sandbox_has_them(scripted):
    def exec_command(cmd: str) -> str:
        """Mine."""
        return cmd

    scripted()
    with pytest.raises(AgentStepError, match="reserved"):
        await _agent(tools=(exec_command,), builtin_tools=["shell"])(a=2, b=3)
    # Without a shell the name is the step's own.
    scripted(_answer(total=5))
    result = await _agent(tools=(exec_command,), builtin_tools=["filesystem"])(a=2, b=3)
    assert result.outputs.total == 5


def test_unknown_model_settings_fail_when_the_runtime_is_built():
    with pytest.raises(ValueError, match="agents.ModelSettings"):
        OpenAIAgent(temprature=0.2)
    assert OpenAIAgent(temperature=0.2).model_extra == {"temperature": 0.2}


def test_the_declaration_merges_the_step_over_the_workflow_default():
    reasoning = Reasoning(effort="low")
    default = OpenAIAgent(
        model=MODEL,
        max_turns=4,
        tracing=True,
        builtin_tools=["shell"],
        temperature=0.1,
        reasoning=reasoning,
    )
    step = OpenAIAgent(max_turns=8, narrate=False)

    assert step.declaration(workflow_default=default) == OpenAIAgentDeclaration(
        model=MODEL,
        settings={
            "max_turns": 8,
            "system_prompt_append": None,
            "narrate": False,
            "tracing": True,
        },
        builtin_tools=("shell",),
    )
    # Model settings keep the SDK objects they were given as.
    assert step.merged_over(default).model_extra == {"temperature": 0.1, "reasoning": reasoning}


@pytest.mark.asyncio
async def test_a_step_without_the_openai_extra_names_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "agents", None)
    monkeypatch.delitem(sys.modules, "avalanche.runtimes.openai_agent._sdk")
    monkeypatch.delattr(importlib.import_module("avalanche.runtimes.openai_agent"), "_sdk")

    with pytest.raises(AgentStepExecutionError, match=r"avalanche-ai\[openai\]"):
        await _agent()(a=2, b=3)


_LIVE = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="set OPENAI_API_KEY to call OpenAI through the OpenAI Agents SDK",
)
LIVE_MODEL = os.environ.get("AVALANCHE_LIVE_OPENAI_MODEL", MODEL)


@_LIVE
@pytest.mark.asyncio
async def test_live_openai_call_with_a_tool():
    class Bonus(ava.Signature):
        """Add a and b, then add the bonus lookup_bonus returns for the name "team", once."""

        a: int = ava.InputField()
        b: int = ava.InputField()
        total: int = ava.OutputField()

    result = await ava.Agent(
        signature=Bonus,
        step_name="total",
        runtime=OpenAIAgent(model=LIVE_MODEL, max_turns=4),
        tools=(lookup_bonus,),
    )(a=2, b=3)

    assert result.outputs.total == 15
    header = result.trace.header
    assert (header.runtime_kind, header.status) == ("openai_agent", "succeeded")
    assert header.usage is not None and header.usage.output_tokens > 0
    assert "lookup_bonus" in [
        call.name for turn in result.trace.turns for call in turn.tool_calls
    ]
    assert result.trace.turns[-1].message


@_LIVE
@pytest.mark.asyncio
async def test_live_openai_reads_a_file_input_and_writes_a_file_output(tmp_path):
    class Upper(ava.Signature):
        """Write the attached document's text in upper case to a new file."""

        document: ava.File = ava.InputField()
        shouted: ava.File = ava.OutputField()

    (tmp_path / "note.txt").write_text("quiet words")
    (tmp_path / "run").mkdir()
    workspace = ava.Workspace.from_directory(tmp_path / "run", reference="snapshot:run")
    agent = ava.Agent(
        signature=Upper, step_name="upper", runtime=OpenAIAgent(model=LIVE_MODEL, max_turns=4)
    )
    with ava.file_reference_context(workspace, attempt="attempt-1"):
        result = await agent(document=ava.File.from_path(tmp_path / "note.txt"))

    assert result.outputs.shouted.read_bytes().decode().strip() == "QUIET WORDS"


@_LIVE
@pytest.mark.asyncio
async def test_live_openai_uses_a_step_skill_and_reads_its_file(tmp_path):
    result = await ava.Agent(
        signature=CodeWord,
        step_name="code_word",
        runtime=OpenAIAgent(model=LIVE_MODEL, max_turns=6),
        skills=(_skill(tmp_path),),
    )(project="heron")

    assert result.outputs.code_word.strip() == "PELICAN-4217"
    calls = [call.name for turn in result.trace.turns for call in turn.tool_calls]
    assert "load_skill" in calls and "read_skill_file" in calls


@_LIVE
@pytest.mark.asyncio
async def test_live_openai_edits_a_workspace_in_its_sandbox(tmp_path):
    class Tidy(ava.Signature):
        """In the workspace, rename todo.txt to done.txt and add the line "checked" at its
        end. Answer with the number of lines done.txt has."""

        workspace: ava.Workspace = ava.InputField()
        lines: int = ava.OutputField()

    (tmp_path / "todo.txt").write_text("write tests\nship\n")
    result = await ava.Agent(
        signature=Tidy,
        step_name="tidy",
        runtime=OpenAIAgent(
            model=LIVE_MODEL, max_turns=8, builtin_tools=["filesystem", "shell"]
        ),
    )(workspace=ava.Workspace.from_directory(tmp_path, reference="todo"))

    assert not (tmp_path / "todo.txt").exists()
    # The model may leave a blank line before the one it adds.
    lines = (tmp_path / "done.txt").read_text().splitlines()
    assert [line for line in lines if line.strip()] == ["write tests", "ship", "checked"]
    assert result.outputs.lines in (3, len(lines))
    assert result.trace.header.tool_call_count > 0
