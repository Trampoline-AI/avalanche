"""Behavior and performance-shape coverage for the agent inspector hierarchy."""

from __future__ import annotations

import json

import pytest
from textual.app import App, ComposeResult

from avalanche.tui.dag_layout import DagNode
from avalanche.tui.mock import MockStateProvider
from avalanche.tui.models import NodeState, NodeStatus, RunState, RunStatus, WorkflowInfo
from avalanche.tui.ui_store import UIStore
from avalanche.tui.widgets.agent_trace import (
    AgentMetadataInspector,
    AgentOutputInspector,
    AgentTraceInspector,
)


class _InspectorHarness(App[None]):
    def __init__(self, store: UIStore) -> None:
        super().__init__()
        self.store = store

    def compose(self) -> ComposeResult:
        yield AgentTraceInspector(id="trace")
        yield AgentOutputInspector(id="output")
        yield AgentMetadataInspector(id="metadata")


def _store(*, steps: list[dict], outputs: dict[str, object]) -> UIStore:
    metadata = {
        "signature": {
            "name": "Inspect",
            "outputs": [
                {"name": name, "annotation": "str", "description": f"{name} value"}
                for name in outputs
            ],
        },
        "runtime": {"max_iterations": 3},
        "skills": [{"name": "audit", "instructions": "Inspect all records."}],
        "aggregated_static_instructions": "Do not skip evidence.",
        "packages": ["pydantic"],
        "modules": ["audit_helpers"],
        "tools": [{"name": "lookup", "description": "Look up a record."}],
    }
    workflow = WorkflowInfo(
        name="agent_flow",
        file_path="agent_flow.py",
        node_ids=["agent"],
        graph={"agent": []},
        node_types={"agent": "step"},
        display_names={"agent": "agent"},
        agent_node_ids=["agent"],
        agent_metadata_json={"agent": json.dumps(metadata)},
    )
    envelope = {
        "status": "completed",
        "run_id": "run-agent",
        "trace": {
            "model": "main",
            "iterations": len(steps),
            "max_iterations": 3,
            "duration_ms": 1,
            "usage": {"main": {}, "sub": {}},
            "steps": steps,
            "evidence": {
                "complete": True,
                "terminal_outcome": "completed",
                "events": [{"kind": "run.succeeded", "data": {"outputs": outputs}}],
            },
        },
    }
    run = RunState(run_id="run-agent", flow_name="agent_flow", status=RunStatus.SUCCESS)
    run.nodes["agent"] = NodeState(
        node_id="agent",
        name="agent",
        node_type="step",
        status=NodeStatus.SUCCESS,
        agent_trace_json=json.dumps(envelope),
    )
    store = UIStore(MockStateProvider())
    store.current_workflow = workflow
    store.current_run = run
    store.selected_node = DagNode("agent", "step")
    assert store.open_trace_inspector()
    return store


def _step(iteration: int, reasoning: object = "reasoning") -> dict:
    return {
        "iteration": iteration,
        "reasoning": reasoning,
        "code": "print('ready')",
        "output": {"answer": "ready"},
        "untruncated_output": {"answer": "full-ready"},
        "duration_ms": 1,
        "tool_calls": [
            {"name": "lookup", "args": ["key"], "kwargs": {}, "result": {"found": True}}
        ],
        "predict_calls": [
            {
                "signature": "question -> answer",
                "model": "sub",
                "calls": [{"input": {"question": "q"}, "output": {"answer": "a"}}],
            }
        ],
        "lm": {"finish_reason": "stop"},
        "usage": {"main": {}, "sub": {}},
    }


@pytest.mark.asyncio
async def test_agent_inspector_hierarchy_navigates_and_expands_each_tab():
    store = _store(steps=[_step(1), _step(2)], outputs={"summary": {"ok": True}})
    app = _InspectorHarness(store)
    async with app.run_test(size=(100, 40)):
        trace = app.query_one("#trace", AgentTraceInspector)
        output = app.query_one("#output", AgentOutputInspector)
        metadata = app.query_one("#metadata", AgentMetadataInspector)

        assert store.trace_selected_path == ("turn", "1")
        assert store.trace_collapsed_turns == {0, 1}
        collapsed = trace.render().plain
        assert "Reasoning" not in collapsed
        assert "print('ready')" not in collapsed
        assert "←/→ tabs · ↑/↓ select · Enter expand/collapse · o full output" in collapsed
        assert "PgUp/PgDn page · Esc back" in collapsed

        store.toggle_trace_turn()
        expanded = trace.render().plain
        assert "Reasoning" in expanded
        assert "Code" in expanded
        assert "Tools (1)" in expanded
        assert "Predict details (1)" in expanded
        assert "print('ready')" not in expanded

        store.move_trace_turn(1)
        assert store.trace_selected_path == ("turn", "1", "reasoning")
        store.toggle_trace_turn()
        assert "reasoning" in trace.render().plain

        store.move_trace_turn(2)
        assert store.trace_selected_path == ("turn", "1", "output")
        store.toggle_trace_turn()
        assert '"answer": "ready"' in trace.render().plain
        store.toggle_trace_full_output()
        assert "Output (full)" in trace.render().plain
        assert '"answer": "full-ready"' in trace.render().plain

        store.move_trace_turn(1)
        assert store.trace_selected_path == ("turn", "1", "tools")
        store.toggle_trace_turn()
        store.move_trace_turn(1)
        assert store.trace_selected_path == ("turn", "1", "tools", "0")
        store.toggle_trace_turn()
        assert "Input" in trace.render().plain
        assert "Result" in trace.render().plain

        store.move_trace_turn(3)
        assert store.trace_selected_path == ("turn", "1", "predict")
        store.toggle_trace_turn()
        store.move_trace_turn(1)
        assert store.trace_selected_path == ("turn", "1", "predict", "0")
        store.toggle_trace_turn()
        assert "Call 1" in trace.render().plain

        store.move_trace_inspector_tab(1)
        assert store.trace_selected_path == ("output", "summary")
        assert '"ok": true' not in output.render().plain
        store.toggle_trace_turn()
        assert '"ok": true' in output.render().plain

        store.move_trace_inspector_tab(1)
        assert store.trace_selected_path == ("metadata", "signature")
        assert "Inspect" not in metadata.render().plain
        store.toggle_trace_turn()
        assert "Inspect" in metadata.render().plain


def test_streamed_turns_default_to_collapsed_without_recollapsing_existing_turns():
    store = _store(steps=[_step(1)], outputs={"summary": {"ok": True}})
    store.toggle_trace_turn()
    assert store.trace_collapsed_turns == set()

    node = store.current_run.nodes["agent"]
    envelope = json.loads(node.agent_trace_json)
    envelope["trace"]["steps"].append(_step(2))
    node.agent_trace_json = json.dumps(envelope)

    assert len(store.selected_agent_steps) == 2
    assert store.trace_collapsed_turns == {1}


@pytest.mark.asyncio
async def test_virtual_body_cache_uses_content_tokens_and_renders_undeclared_outputs(
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(
        steps=[_step(1)],
        outputs={
            "summary": {"ok": True},
            "note": None,
            "undeclared": {"included": True},
        },
    )
    metadata = json.loads(store.current_workflow.agent_metadata_json["agent"])
    metadata["signature"]["outputs"] = metadata["signature"]["outputs"][:2]
    store.current_workflow.agent_metadata_json["agent"] = json.dumps(metadata)
    app = _InspectorHarness(store)
    from avalanche.tui.widgets import agent_trace

    original_json = agent_trace._json
    calls: list[object] = []

    def record_json(value: object) -> str:
        calls.append(value)
        return original_json(value)

    monkeypatch.setattr(agent_trace, "_json", record_json)
    async with app.run_test(size=(100, 20)):
        output = app.query_one("#output", AgentOutputInspector)
        store.move_trace_inspector_tab(1)
        store.toggle_trace_turn()
        first_render = output.render().plain
        assert '"ok": true' in first_render
        assert "undeclared" in first_render
        assert len(calls) == 1

        output.render()
        assert len(calls) == 1

        store.trace_selected_paths["output"] = ("output", "note")
        store.toggle_trace_turn()
        assert "null" in output.render().plain

        node = store.current_run.nodes["agent"]
        envelope = json.loads(node.agent_trace_json)
        del envelope["trace"]["evidence"]["events"][0]["data"]["outputs"]["note"]
        node.agent_trace_json = json.dumps(envelope)
        assert "Unavailable" in output.render().plain

        envelope = json.loads(node.agent_trace_json)
        envelope["trace"]["evidence"]["events"][0]["data"]["outputs"]["summary"] = {
            "updated": True
        }
        node.agent_trace_json = json.dumps(envelope)
        assert '"updated": true' in output.render().plain
        assert len(calls) >= 2


@pytest.mark.asyncio
async def test_hidden_inspector_tabs_skip_trace_hydration(monkeypatch: pytest.MonkeyPatch):
    store = _store(steps=[_step(1)], outputs={"summary": {"ok": True}})
    store.close_trace_inspector()
    app = _InspectorHarness(store)

    def unexpected_trace_read(_: UIStore) -> dict:
        raise AssertionError("hidden inspector decoded its trace")

    monkeypatch.setattr(
        UIStore,
        "selected_agent_trace_envelope",
        property(unexpected_trace_read),
    )
    async with app.run_test(size=(100, 20)):
        assert app.query_one("#trace", AgentTraceInspector).render().plain == ""
        assert app.query_one("#output", AgentOutputInspector).render().plain == ""
        assert app.query_one("#metadata", AgentMetadataInspector).render().plain == ""


@pytest.mark.asyncio
async def test_large_hierarchy_reuses_index_and_renders_only_viewport_rows(
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(
        steps=[_step(index + 1) for index in range(2_000)],
        outputs={"summary": {"ok": True}},
    )
    app = _InspectorHarness(store)
    calls = 0
    original_rows = AgentTraceInspector._rows.__func__

    def count_rows(cls, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_rows(cls, *args, **kwargs)

    monkeypatch.setattr(AgentTraceInspector, "_rows", classmethod(count_rows))
    async with app.run_test(size=(100, 20)):
        calls = 0
        trace = app.query_one("#trace", AgentTraceInspector)
        trace._layout = None
        first = trace.render().plain
        second = trace.render().plain
        assert calls == 1
        assert first == second
        assert first.count("AGENT TURN") < 40


@pytest.mark.asyncio
async def test_collapsed_and_offscreen_bodies_are_never_formatted(
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(
        steps=[_step(1, {"large": ["payload"] * 50_000})],
        outputs={"summary": {"large": ["output"] * 50_000}},
    )
    app = _InspectorHarness(store)
    calls: list[object] = []

    def record_json(value: object) -> str:
        calls.append(value)
        return "unexpected render"

    monkeypatch.setattr("avalanche.tui.widgets.agent_trace._json", record_json)
    async with app.run_test(size=(100, 20)):
        trace = app.query_one("#trace", AgentTraceInspector)
        assert "payload" not in trace.render().plain
        assert calls == []

        store.toggle_trace_turn()
        store.move_trace_turn(1)
        assert store.trace_selected_path == ("turn", "0", "reasoning")
        store.toggle_trace_turn()
        monkeypatch.setattr(trace, "_viewport", lambda: (10_000, 10))
        trace.render()
        assert calls == []
