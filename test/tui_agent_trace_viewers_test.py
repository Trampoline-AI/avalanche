"""Every agent trace format the operator can send has a TUI viewer."""

from __future__ import annotations

import importlib
from typing import get_args

import pytest

from avalanche.agent.native import NativeEvent
from avalanche.agent.trace import NativeTrace
from tui.agent_trace_viewers import VIEWERS


def _members(union: object) -> list[type]:
    """The model classes of a (possibly nested) discriminated union."""
    args = get_args(union)
    if not args:
        return [union]
    return [member for arg in args for member in _members(arg) if isinstance(member, type)]


def _formats(union: object) -> set[str]:
    return {member.model_fields["format"].default for member in _members(union)}


def test_every_trace_format_has_a_viewer():
    assert set(VIEWERS) == _formats(NativeEvent) | _formats(NativeTrace)


def test_the_predict_rlm_viewer_labels_every_native_kind():
    from avalanche.agent.native import predict_rlm as native
    from tui.agent_trace_viewers.predict_rlm import _LABELS

    members = _members(native.PredictRLMNativeEvent)
    assert set(_LABELS) == {kind for member in members for kind in member.common_kinds}


def test_the_claude_agent_viewer_labels_every_native_kind():
    from avalanche.agent.native import claude_agent as native
    from tui.agent_trace_viewers.claude_agent import _LABELS

    members = _members(native.ClaudeAgentNativeEvent)
    assert set(_LABELS) == {kind for member in members for kind in member.common_kinds}


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
            structured_output={"total": 5},
        )

    monkeypatch.setattr(sdk, "query", query)
    sent = []
    agent = ava.Agent(
        signature=Sum, step_name="sum", runtime=ClaudeAgent(model="claude-haiku-5-5")
    )
    with capture_agent_evidence(sent.append, errors="raise"):
        asyncio.run(agent(a=2, b=3))

    *events, final = sent
    viewer = viewer_for(final.record.header.trace_format)
    turn_event = next(event for event in events if event.common is not None)
    view = viewer.turn(
        InspectionTurn(turn_event.invocation_id, turn_event.common, turn_event.native),
        full_output=False,
    )
    assert [item.key for item in view.items] == ["text", "tools", "models", "metadata"]
    assert any("session-1" in line for line, _ in viewer.header(final.record))
    assert viewer.outputs(events) == {"total": 5}
    assert {viewer.live_label(event) for event in events}
