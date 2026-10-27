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


def _formats(union: object) -> set[str]:
    return {member.model_fields["format"].default for member in _members(union)}


def test_every_trace_format_has_a_viewer():
    assert set(VIEWERS) == _formats(NativeEvent) | _formats(TraceHeaderNative) | _formats(
        TurnNative
    )


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

    monkeypatch.setattr(sdk, "_session", query)
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
    assert any("session-1" in line for line, _ in viewer.header(final.record))
    assert viewer.outputs(events) == {"total": 5}
    # A successful output too large for the trace still shows as present.
    finished = next(event for event in events if event.native.kind == "finished")
    omitted = finished.model_copy(
        update={
            "native": finished.native.model_copy(
                update={"structured_output": None, "output_omitted": True}
            )
        }
    )
    assert viewer.outputs([omitted]) is not None
    assert {viewer.live_label(event) for event in events}
