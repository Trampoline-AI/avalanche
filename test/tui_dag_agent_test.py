"""Behavior tests for agent affordances in the DAG view."""

from __future__ import annotations

import pytest

from avalanche.tui.app import AvalancheApp
from avalanche.tui.dag_layout import render_dag_rich, workflow_to_layout
from avalanche.tui.models import NodeStatus, WorkflowInfo
from avalanche.tui.theme import AGENT_MARKER


def _workflow_with_agent_step() -> WorkflowInfo:
    return WorkflowInfo(
        name="agent_affordances",
        file_path="agent_affordances.py",
        node_ids=["inspect_1", "agent_named_but_normal_1"],
        graph={"inspect_1": ["agent_named_but_normal_1"]},
        node_types={
            "inspect_1": "step",
            "agent_named_but_normal_1": "step",
        },
        display_names={
            "inspect_1": "inspect",
            "agent_named_but_normal_1": "agent_named_but_normal",
        },
        agent_node_ids=["inspect_1"],
    )


def test_agent_marker_uses_workflow_agent_node_ids_and_preserves_statuses():
    workflow = _workflow_with_agent_step()
    dag, nodes = workflow_to_layout(workflow)
    nodes_by_id = {node.name: node for node in nodes}

    assert nodes_by_id["inspect_1"].is_agent
    assert not nodes_by_id["agent_named_but_normal_1"].is_agent

    statuses = {node.name: NodeStatus.SUCCESS for node in nodes}
    rendered = "\n".join(line.plain for line in render_dag_rich(dag, statuses, 0, None))

    assert f"✓ {AGENT_MARKER} inspect" in rendered
    assert "✓ agent_named_but_normal" in rendered
    assert f"{AGENT_MARKER} agent_named_but_normal" not in rendered


@pytest.mark.asyncio
async def test_agent_dag_hint_explains_selection_and_inspector_activation():
    app = AvalancheApp(workflow="agent_trace")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        dag = app._screen.query_one("#dag-panel")
        rendered = dag.render().plain

        assert "Click or ↑↓←→ select node" in rendered
        assert "Enter inspect selected agent step" in rendered
        assert f"{AGENT_MARKER} agent step" in rendered

        agent_node = next(node for node in app.store.all_nodes if node.is_agent)
        app.select_node(agent_node)
        await pilot.press("enter")
        await pilot.pause()

        assert app.store.trace_inspector_open
