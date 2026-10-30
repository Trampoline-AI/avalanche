"""Every agent trace format the operator can send has a TUI viewer."""

from __future__ import annotations

import importlib
from typing import get_args

import pytest

from avalanche.agent.native import NativeEvent, TraceHeaderNative, TurnNative
from tui.agent_trace_viewers import VIEWERS


def _members(union: object) -> list[type]:
    """The model classes of a (possibly nested) discriminated union."""
    args = get_args(union)
    if not args:
        return [union]
    return [member for arg in args for member in _members(arg) if isinstance(member, type)]


def _kinds(union: object) -> set[str]:
    """The native kinds a union's members can carry."""
    return {
        kind
        for member in _members(union)
        for kind in get_args(member.model_fields["kind"].annotation)
    }


def _formats(union: object) -> set[str]:
    return {member.model_fields["format"].default for member in _members(union)}


def test_every_trace_format_has_a_viewer():
    assert set(VIEWERS) == _formats(NativeEvent) | _formats(TraceHeaderNative) | _formats(
        TurnNative
    )


def test_the_predict_rlm_viewer_labels_every_native_kind():
    from avalanche.agent.native import predict_rlm as native
    from tui.agent_trace_viewers.predict_rlm import _LABELS

    assert set(_LABELS) == _kinds(native.PredictRLMNativeEvent)


def test_the_claude_agent_viewer_labels_every_native_kind():
    from avalanche.agent.native import claude_agent as native
    from tui.agent_trace_viewers.claude_agent import _LABELS

    assert set(_LABELS) == _kinds(native.ClaudeAgentNativeEvent)


def test_the_pydantic_ai_viewer_labels_every_native_kind():
    from avalanche.agent.native import pydantic_ai as native
    from tui.agent_trace_viewers.pydantic_ai import _LABELS

    assert set(_LABELS) == _kinds(native.PydanticAINativeEvent)


def test_a_pydantic_ai_run_renders_through_its_viewer(monkeypatch):
    import asyncio

    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    import avalanche as ava
    from avalanche._agent_evidence import capture_agent_evidence
    from avalanche.runtimes import PydanticAIAgent
    from tui.agent_trace_viewers import InspectionTurn, viewer_for

    sdk = importlib.import_module("avalanche.runtimes.pydantic_ai._sdk")

    class Sum(ava.Signature):
        """Add a and b."""

        a: int = ava.InputField()
        b: int = ava.InputField()
        total: int = ava.OutputField()

    responses = [
        # A first answer that fails validation is retried.
        ModelResponse(
            parts=[ToolCallPart("final_result", {"total": "five"}, tool_call_id="a")]
        ),
        ModelResponse(
            parts=[TextPart("It is 5."), ToolCallPart("final_result", {"total": 5})],
            finish_reason="error",
        ),
    ]

    class Scripted(Agent):
        def __init__(self, model, **options):
            super().__init__(FunctionModel(lambda messages, info: responses.pop(0)), **options)

    monkeypatch.setattr(sdk, "Agent", Scripted)
    sent = []
    agent = ava.Agent(
        signature=Sum, step_name="sum", runtime=PydanticAIAgent(model="openai:gpt-5.4-nano")
    )
    with capture_agent_evidence(sent.append, errors="raise"):
        asyncio.run(agent(a=2, b=3))

    *events, final = sent
    viewer = viewer_for(final.header.trace_format)
    turns = [event for event in events if event.common and event.common.kind == "turn.finished"]
    views = [
        viewer.turn(
            InspectionTurn(event.invocation_id, event.common, event.native), full_output=False
        )
        for event in turns
    ]
    assert [item.key for item in views[1].items] == ["message", "tools", "metadata", "response"]
    # A response that ended in a provider error marks its turn failed.
    assert [view.failed for view in views] == [False, True]
    header = [line for line, _ in viewer.header(final.header)]
    # The run ID is in the shared header; this one adds the conversation.
    assert final.header.session_id is not None
    assert any(final.header.session_id in line for line in header)
    assert not any(final.header.external_run_id in line for line in header)
    # A retried call adds a header line carrying the count.
    native = final.header.native
    assert native.retries == 1
    unretried = final.header.model_copy(
        update={"native": native.model_copy(update={"retries": 0})}
    )
    (retry_line,) = set(header) - {line for line, _ in viewer.header(unretried)}
    assert str(native.retries) in retry_line
    labels = [viewer.live_label(event) for event in events]
    assert all(labels)
    assert len(set(labels)) == len({event.kind_label for event in events})


def test_a_claude_session_renders_through_its_viewer_and_metadata_sections(monkeypatch):
    import asyncio

    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    import avalanche as ava
    from avalanche._agent_evidence import capture_agent_evidence
    from avalanche.runtimes import ClaudeAgent
    from tui.agent_trace_viewers import InspectionTurn, viewer_for

    sdk = importlib.import_module("avalanche.runtimes.claude_agent._sdk")

    class Sum(ava.Signature):
        """Add a and b."""

        a: int = ava.InputField()
        b: int = ava.InputField()
        total: int = ava.OutputField()

    async def query(*, prompt, options):
        yield AssistantMessage(content=[TextBlock(text="It is 5.")], model="claude-haiku-5-5")
        yield ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=8,
            is_error=False,
            num_turns=1,
            session_id="session-1",
            structured_output={"total": 5, "run_summary": "Added 2 and 3."},
        )

    monkeypatch.setattr(sdk, "_session", query)
    sent = []
    agent = ava.Agent(
        signature=Sum, step_name="sum", runtime=ClaudeAgent(model="claude-haiku-5-5")
    )
    with capture_agent_evidence(sent.append, errors="raise"):
        asyncio.run(agent(a=2, b=3))

    *events, final = sent
    viewer = viewer_for(final.header.trace_format)
    turn_event = next(event for event in events if event.common.kind == "turn.finished")
    view = viewer.turn(
        InspectionTurn(turn_event.invocation_id, turn_event.common, turn_event.native),
        full_output=False,
    )
    assert [item.key for item in view.items] == ["message", "tools", "metadata", "response"]
    assert not view.failed
    # A rate-limit, billing or sign-in failure marks the turn failed.
    turn = turn_event.common.turn
    failed_turn = turn.model_copy(
        update={"native": turn.native.model_copy(update={"api_error": "rate_limit"})}
    )
    api_error = viewer.turn(
        InspectionTurn(
            turn_event.invocation_id,
            turn_event.common.model_copy(update={"turn": failed_turn}),
            turn_event.native,
        ),
        full_output=False,
    )
    assert api_error.failed
    # Provider reasoning comes last, after everything a reader looks at first.
    thinking = turn_event.common.model_copy(
        update={"turn": turn_event.common.turn.model_copy(update={"reasoning": "hmm"})}
    )
    view = viewer.turn(
        InspectionTurn(turn_event.invocation_id, thinking, turn_event.native),
        full_output=False,
    )
    assert view.items[0].key == "message"
    assert view.items[-1].key == "reasoning"
    assert any("session-1" in line for line, _ in viewer.header(final.header))
    assert final.outputs == {"total": 5}
    # Events without a Claude part are named from their common kind.
    assert all(viewer.live_label(event) for event in events)


def test_an_openai_run_renders_through_its_viewer(monkeypatch):
    import asyncio
    import contextlib
    import json

    pytest.importorskip("agents")
    from agents.models.interface import ModelProvider
    from agents.testing import ModelStep, ScriptedModel, assistant_message

    import avalanche as ava
    from avalanche._agent_evidence import capture_agent_evidence
    from avalanche.agent.native import openai_agent as native
    from avalanche.runtimes import OpenAIAgent
    from tui.agent_trace_viewers import InspectionTurn, viewer_for

    sdk = importlib.import_module("avalanche.runtimes.openai_agent._sdk")

    class Sum(ava.Signature):
        """Add a and b."""

        a: int = ava.InputField()
        b: int = ava.InputField()
        total: int = ava.OutputField()

    answer = json.dumps({"total": 5, "run_summary": "Added 2 and 3."})
    model = ScriptedModel([ModelStep(output=[assistant_message(answer)])])

    class Scripted(ModelProvider):
        def get_model(self, model_name):
            return model

    @contextlib.asynccontextmanager
    async def models():
        yield Scripted()

    monkeypatch.setattr(sdk, "_models", models)
    sent = []
    agent = ava.Agent(signature=Sum, step_name="sum", runtime=OpenAIAgent(max_turns=3))
    with capture_agent_evidence(sent.append, errors="raise"):
        asyncio.run(agent(a=2, b=3))

    *events, final = sent
    viewer = viewer_for(final.header.trace_format)
    turn_event = next(
        event
        for event in events
        if event.common is not None and event.common.kind == "turn.finished"
    )
    view = viewer.turn(
        InspectionTurn(turn_event.invocation_id, turn_event.common, turn_event.native),
        full_output=False,
    )
    assert [item.key for item in view.items] == ["message", "tools", "metadata", "response"]
    assert not view.failed
    # A refusal marks the turn failed.
    turn = turn_event.common.turn
    refused = turn.model_copy(
        update={"native": turn.native.model_copy(update={"refusal": "No."})}
    )
    assert viewer.turn(
        InspectionTurn(
            turn_event.invocation_id,
            turn_event.common.model_copy(update={"turn": refused}),
            turn_event.native,
        ),
        full_output=False,
    ).failed
    assert viewer.turn_limit(final.header) == 3
    assert viewer.turn_limit(None) is None
    assert viewer.submitted(final.header)
    assert viewer.header(final.header)
    # The run sends every native kind, and the viewer names each event.
    assert {e.native.kind for e in events if e.native} == _kinds(native.OpenAIAgentNativeEvent)
    assert all(viewer.live_label(event) for event in events)


def test_usage_counts_prompt_cache_input():
    from avalanche.agent.trace import TokenUsage
    from tui.agent_trace_viewers.generic import usage_label

    cached = TokenUsage(
        input_tokens=3,
        output_tokens=5,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=40,
    )
    uncached = TokenUsage(input_tokens=3, output_tokens=5)

    assert usage_label(cached).startswith("943 in / 5 out")
    assert usage_label(uncached).startswith("3 in / 5 out")
