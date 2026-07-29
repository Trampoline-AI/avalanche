"""Behavior and performance-shape coverage for the agent inspector hierarchy."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding

from avalanche.tui.app import AvalancheApp
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


class _CountingMapping(Mapping[str, object]):
    def __init__(self, size: int) -> None:
        self.size = size
        self.items_yielded = 0

    def __getitem__(self, key: str) -> object:
        return {"payload": key}

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return self.size

    def items(self):
        for index in range(self.size):
            self.items_yielded += 1
            yield f"item-{index}", {"payload": index}


class _CountingSequence(Sequence[str]):
    def __init__(self, size: int) -> None:
        self.size = size
        self.items_read = 0

    def __getitem__(self, index: int) -> str:
        self.items_read += 1
        return f"value-{index}"

    def __len__(self) -> int:
        return self.size


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
        assert "[Enter] Expand/Collapse" in collapsed
        assert "[e] Expand all under selection" in collapsed
        assert "[z] Collapse all under selection" in collapsed

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
        controls = metadata.render().plain
        assert "[e] Expand all under selection" in controls
        assert "[z] Collapse all under selection" in controls


def test_inspector_expand_binding_uses_non_conflicting_e_key():
    bindings = [binding for binding in AvalancheApp.BINDINGS if isinstance(binding, Binding)]
    expand_actions = {
        binding.key for binding in bindings if binding.action == "expand_trace_hierarchy"
    }
    assert expand_actions == {"e"}


def test_recursive_expand_and_collapse_are_scoped_to_selected_object():
    store = _store(
        steps=[],
        outputs={
            "summary": [
                {"selected": {"nested": [1, 2]}},
                {"sibling": {"must_stay_closed": True}},
            ]
        },
    )
    store.move_trace_inspector_tab(1)
    store.toggle_trace_turn()
    store.move_trace_turn(1)
    selected = ("output", "summary", "index", "0")
    sibling = ("output", "summary", "index", "1")
    assert store.trace_selected_path == selected

    store.expand_trace_hierarchy()
    selected_child = selected + ("key", "selected")
    sibling_child = sibling + ("key", "sibling")
    paths = store.trace_inspector_navigation_paths()
    assert selected_child in paths
    assert sibling_child not in paths
    assert store.trace_path_expanded(selected_child)
    assert not store.trace_path_expanded(sibling_child)

    store.move_trace_turn(1)
    assert store.trace_selected_path == selected_child
    store.collapse_trace_hierarchy()
    assert store.trace_selected_path == selected_child
    assert selected_child in store.trace_collapsed_items
    assert store.trace_path_expanded(selected)


@pytest.mark.asyncio
async def test_trace_status_infers_success_and_durations_render_in_seconds():
    store = _store(steps=[_step(1)], outputs={"summary": "ready"})
    envelope = json.loads(store.current_run.nodes["agent"].agent_trace_json)
    envelope.pop("status")
    envelope["trace"].pop("status", None)
    envelope["trace"]["duration_ms"] = 12_500
    envelope["trace"]["steps"][0]["duration_ms"] = 2_450
    store.current_run.nodes["agent"].agent_trace_json = json.dumps(envelope)
    app = _InspectorHarness(store)

    async with app.run_test(size=(100, 30)):
        rendered = app.query_one("#trace", AgentTraceInspector).render().plain
        assert "Status: completed" in rendered
        assert "Duration: 12.5s" in rendered
        assert "AGENT TURN 1/3 · 2.5s" in rendered
        assert "ms" not in rendered


@pytest.mark.asyncio
async def test_metadata_pane_distinguishes_unpublished_and_malformed_payloads():
    store = _store(steps=[], outputs={})
    store.move_trace_inspector_tab(2)
    app = _InspectorHarness(store)

    async with app.run_test(size=(100, 20)):
        metadata = app.query_one("#metadata", AgentMetadataInspector)
        store.current_workflow.agent_metadata_json.clear()
        assert "Restart the operator" in metadata.render().plain

        store.current_workflow.agent_metadata_json["agent"] = "{"
        assert "Metadata payload is malformed" in metadata.render().plain


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


def test_live_evidence_preserves_reasoning_and_terminal_outputs():
    store = _store(steps=[], outputs={})
    node = store.current_run.nodes["agent"]
    node.status = NodeStatus.RUNNING
    node.agent_trace_json = json.dumps(
        {
            "events": [
                {
                    "kind": "iteration.recorded",
                    "data": {
                        "step": {
                            "iteration": 1,
                            "reasoning": "Captured before hydration.",
                        }
                    },
                }
            ]
        }
    )
    assert store.selected_agent_inspector_state == "live"
    assert store.selected_agent_live_steps[0]["reasoning"] == "Captured before hydration."

    node.status = NodeStatus.SUCCESS
    node.agent_trace_json = json.dumps(
        {
            "events": [
                {
                    "kind": "run.succeeded",
                    "data": {"status": "completed", "outputs": {"summary": "ready"}},
                }
            ]
        }
    )
    assert store.selected_agent_inspector_state == "completed_with_output"
    assert store.selected_agent_outputs == {"summary": "ready"}


def test_inspector_states_distinguish_delay_completion_and_malformed_data():
    store = _store(steps=[], outputs={})
    node = store.current_run.nodes["agent"]
    node.agent_trace_json = None
    node.status = NodeStatus.PENDING
    assert store.selected_agent_inspector_state == "pending"
    node.status = NodeStatus.SUCCESS
    assert store.selected_agent_inspector_state == "completed_without_output"
    node.agent_trace_json = "{"
    assert store.selected_agent_inspector_state == "malformed"


def test_hydration_state_yields_to_installed_trace_body():
    from avalanche.operator.models import TraceDescriptor

    store = _store(steps=[], outputs={"summary": "ready"})
    node = store.current_run.nodes["agent"]
    node.trace = TraceDescriptor(available=True)
    assert store.selected_agent_inspector_state == "completed_with_output"

    node.agent_trace_json = None
    assert store.selected_agent_inspector_state == "hydrating"


def test_reasoning_navigation_includes_nested_values():
    store = _store(steps=[_step(1, {"nested": {"values": [1, 2]}})], outputs={})
    store.toggle_trace_turn()
    store.move_trace_turn(1)
    assert store.trace_selected_path == ("turn", "0", "reasoning")
    store.toggle_trace_turn()
    assert ("turn", "0", "reasoning", "key", "nested") in (
        store.trace_inspector_navigation_paths()
    )


@pytest.mark.asyncio
async def test_trace_rendering_distinguishes_pending_live_failed_malformed_and_incomplete():
    store = _store(steps=[_step(1)], outputs={"summary": "ready"})
    node = store.current_run.nodes["agent"]

    def set_trace(status: NodeStatus, trace_json: str | None) -> None:
        node.status = status
        node.agent_trace_json = trace_json
        store._invalidate_agent_event_details(store.current_run, "agent")
        store._touch_trace_hierarchy()

    app = _InspectorHarness(store)
    async with app.run_test(size=(100, 20)):
        trace = app.query_one("#trace", AgentTraceInspector)
        set_trace(NodeStatus.PENDING, None)
        assert "This agent step has not run yet" in trace.render().plain

        set_trace(NodeStatus.RUNNING, None)
        assert "waiting for live updates" in trace.render().plain

        set_trace(
            NodeStatus.RUNNING,
            json.dumps(
                {
                    "events": [
                        {
                            "kind": "code.generated",
                            "data": {"iteration": 1, "code": "print('live')"},
                        }
                    ]
                }
            ),
        )
        assert "LIVE TRACE · 1 turn(s)" in trace.render().plain

        set_trace(NodeStatus.FAILED, None)
        assert "Agent failed before a structured trace" in trace.render().plain

        set_trace(NodeStatus.SUCCESS, "{")
        assert "Trace is malformed." in trace.render().plain

        completed_trace = _store(steps=[_step(1)], outputs={"summary": "ready"})
        set_trace(
            NodeStatus.FAILED,
            completed_trace.current_run.nodes["agent"].agent_trace_json,
        )
        failed_trace = trace.render().plain
        assert "Status: failed" in failed_trace
        assert "submitted" not in failed_trace

        incomplete = json.loads(
            _store(steps=[_step(1)], outputs={"summary": "ready"})
            .current_run.nodes["agent"]
            .agent_trace_json
        )
        incomplete["trace"]["evidence"]["complete"] = False
        set_trace(NodeStatus.SUCCESS, json.dumps(incomplete))
        assert "Live record: incomplete" in trace.render().plain


@pytest.mark.asyncio
async def test_live_reasoning_stays_hidden_until_iteration_is_recorded():
    store = _store(steps=[], outputs={})
    node = store.current_run.nodes["agent"]
    node.status = NodeStatus.RUNNING
    node.agent_trace_json = json.dumps(
        {
            "events": [
                {
                    "kind": "code.generated",
                    "data": {"iteration": 1, "code": "print('live')"},
                }
            ]
        }
    )
    store.trace_collapsed_turns.clear()
    store.trace_selected_paths["trace"] = ("turn", "0")
    app = _InspectorHarness(store)
    async with app.run_test(size=(100, 20)):
        trace = app.query_one("#trace", AgentTraceInspector)
        assert "Reasoning" not in trace.render().plain

        node.agent_trace_json = json.dumps(
            {
                "events": [
                    {
                        "kind": "iteration.recorded",
                        "data": {
                            "step": {
                                "iteration": 1,
                                "reasoning": "Captured reasoning.",
                            }
                        },
                    }
                ]
            }
        )
        store._invalidate_agent_event_details(store.current_run, "agent")
        store._touch_trace_hierarchy()
        assert "Reasoning" in trace.render().plain


@pytest.mark.asyncio
async def test_expand_all_keeps_large_list_layout_and_work_bounded(
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(steps=[], outputs={"summary": {}})
    sequence = _CountingSequence(50_000)
    monkeypatch.setattr(
        UIStore,
        "selected_agent_outputs",
        property(lambda _: {"summary": sequence}),
    )
    app = _InspectorHarness(store)
    async with app.run_test(size=(100, 20)):
        store.move_trace_inspector_tab(1)
        store.expand_trace_hierarchy()
        output = app.query_one("#output", AgentOutputInspector)
        output.render()
        assert output._layout is not None
        assert len(output._layout.rows) < 250
        assert sequence.items_read == 0

        store.move_trace_turn(1)
        output.render()
        assert output._layout is not None
        assert len(output._layout.rows) < 250
        assert sequence.items_read == 0

        store.move_trace_turn(1)
        output.render()
        assert output._layout is not None
        assert len(output._layout.rows) < 250
        assert sequence.items_read <= 10


@pytest.mark.asyncio
async def test_expand_all_lazily_slices_large_mapping_pages(
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(steps=[], outputs={"summary": {}})
    mapping = _CountingMapping(50_000)
    monkeypatch.setattr(
        UIStore,
        "selected_agent_outputs",
        property(lambda _: {"summary": mapping}),
    )
    app = _InspectorHarness(store)
    async with app.run_test(size=(100, 20)):
        store.move_trace_inspector_tab(1)
        store.expand_trace_hierarchy()
        output = app.query_one("#output", AgentOutputInspector)
        output.render()
        assert output._layout is not None
        assert len(output._layout.rows) < 250
        assert mapping.items_yielded == 0

        store.move_trace_turn(1)
        output.render()
        assert output._layout is not None
        assert len(output._layout.rows) < 250
        assert mapping.items_yielded == 0

        store.move_trace_turn(1)
        output.render()
        assert output._layout is not None
        assert len(output._layout.rows) < 250
        assert mapping.items_yielded <= 10
        next_page = (
            "output",
            "summary",
            "range",
            "0",
            "500",
            "range",
            "5",
            "10",
        )
        store._set_trace_selection(next_page)
        store._touch_trace_hierarchy()
        output.render()
        assert mapping.items_yielded == 10

        distant_page = (
            "output",
            "summary",
            "range",
            "49500",
            "50000",
            "range",
            "49500",
            "49505",
        )
        store._set_trace_selection(distant_page)
        store._touch_trace_hierarchy()
        output.render()
        assert output._layout is not None
        assert any("Visit earlier mapping pages" in row.label for row in output._layout.rows)
        assert mapping.items_yielded <= 10


def test_expanding_one_tab_preserves_other_tab_manual_collapses():
    store = _store(steps=[], outputs={"summary": {"ok": True}})
    store.move_trace_inspector_tab(2)
    metadata_path = ("metadata", "signature")
    assert store.trace_selected_path == metadata_path
    store.expand_trace_hierarchy()
    store.toggle_trace_turn()
    assert metadata_path in store.trace_collapsed_items

    store.move_trace_inspector_tab(-1)
    store.expand_trace_hierarchy()
    assert metadata_path in store.trace_collapsed_items
    store.move_trace_inspector_tab(1)
    assert not store.trace_path_expanded(metadata_path)


@pytest.mark.asyncio
async def test_mapping_key_named_range_preserves_page_cache_identity(
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(steps=[], outputs={"summary": {}})
    mapping = _CountingMapping(50_000)
    monkeypatch.setattr(
        UIStore,
        "selected_agent_outputs",
        property(lambda _: {"summary": {"range": mapping}}),
    )
    app = _InspectorHarness(store)
    async with app.run_test(size=(100, 20)):
        store.move_trace_inspector_tab(1)
        store.expand_trace_hierarchy()
        output = app.query_one("#output", AgentOutputInspector)
        for _ in range(3):
            store.move_trace_turn(1)
            output.render()
        assert mapping.items_yielded == 5

        next_page = (
            "output",
            "summary",
            "key",
            "range",
            "range",
            "0",
            "500",
            "range",
            "5",
            "10",
        )
        store._set_trace_selection(next_page)
        store._touch_trace_hierarchy()
        output.render()
        assert mapping.items_yielded == 10


@pytest.mark.asyncio
async def test_completed_rlm_marks_only_the_final_turn_submitted():
    store = _store(steps=[_step(1), _step(2)], outputs={"summary": "ready"})
    envelope = json.loads(store.current_run.nodes["agent"].agent_trace_json)
    envelope["trace"]["status"] = "completed"
    store.current_run.nodes["agent"].agent_trace_json = json.dumps(envelope)
    store.current_run.status = RunStatus.RUNNING
    app = _InspectorHarness(store)
    async with app.run_test(size=(100, 20)):
        trace = app.query_one("#trace", AgentTraceInspector)
        assert trace.render().plain.count("submitted") == 1

        node = store.current_run.nodes["agent"]
        node.status = NodeStatus.FAILED
        assert "submitted" not in trace.render().plain

        node.status = NodeStatus.SKIPPED
        assert "submitted" not in trace.render().plain
