"""Behavior tests for agent affordances in the DAG view."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tui.app import AvalancheApp
from tui.dag_layout import render_dag_rich, workflow_to_layout
from tui.mock import MockStateProvider
from tui.models import NodeStatus, WorkflowInfo
from tui.theme import AGENT_CAPTION_STYLE
from tui.widgets.dag import DagWidget


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


def test_agent_caption_uses_workflow_agent_node_ids_not_step_names():
    workflow = _workflow_with_agent_step()
    dag, nodes = workflow_to_layout(workflow)
    nodes_by_id = {node.name: node for node in nodes}

    assert nodes_by_id["inspect_1"].is_agent
    assert not nodes_by_id["agent_named_but_normal_1"].is_agent

    statuses = {node.name: NodeStatus.SUCCESS for node in nodes}
    lines = render_dag_rich(dag, statuses, 0, None)
    inspect = nodes_by_id["inspect_1"]
    assert inspect.render_row is not None
    assert inspect.caption_render_row == inspect.render_row + 1
    assert "(agent)" not in lines[inspect.render_row].plain
    assert "(agent)" in lines[inspect.caption_render_row].plain
    assert "agent_named_but_normal" in lines[inspect.render_row].plain
    assert "(agent)" not in "\n".join(
        line.plain for line in lines if "agent_named_but_normal" in line.plain
    )
    assert any(
        span.style == AGENT_CAPTION_STYLE for span in lines[inspect.caption_render_row].spans
    )


def _workflow_with_parallel_agent_steps() -> WorkflowInfo:
    return WorkflowInfo(
        name="parallel_agent_captions",
        file_path="parallel_agent_captions.py",
        node_ids=["source_1", "review_1", "publish_1", "join_1"],
        graph={
            "source_1": ["review_1", "publish_1", "join_1"],
            "review_1": ["join_1"],
            "publish_1": ["join_1"],
        },
        node_types={
            node_id: "step" for node_id in ["source_1", "review_1", "publish_1", "join_1"]
        },
        display_names={
            "source_1": "source",
            "review_1": "review",
            "publish_1": "publish",
            "join_1": "join",
        },
        agent_node_ids=["source_1", "review_1", "join_1"],
    )


def _workflow_with_duplicate_agent_steps() -> WorkflowInfo:
    return WorkflowInfo(
        name="duplicate_agent_captions",
        file_path="duplicate_agent_captions.py",
        node_ids=["inspect_first_1", "inspect_second_1"],
        graph={"inspect_first_1": ["inspect_second_1"]},
        node_types={"inspect_first_1": "step", "inspect_second_1": "step"},
        display_names={"inspect_first_1": "inspect", "inspect_second_1": "inspect"},
        agent_node_ids=["inspect_first_1", "inspect_second_1"],
    )


def _workflow_with_cross_track_agent_skip() -> WorkflowInfo:
    return WorkflowInfo(
        name="cross_track_agent_captions",
        file_path="cross_track_agent_captions.py",
        node_ids=["complex_root_1", "agent_root_1", "left_1", "right_1", "join_agent_1"],
        graph={
            "complex_root_1": ["left_1", "right_1"],
            "agent_root_1": ["join_agent_1"],
            "left_1": ["join_agent_1"],
            "right_1": ["join_agent_1"],
        },
        node_types={
            node_id: "step"
            for node_id in [
                "complex_root_1",
                "agent_root_1",
                "left_1",
                "right_1",
                "join_agent_1",
            ]
        },
        display_names={
            "complex_root_1": "complex_root",
            "agent_root_1": "agent_root",
            "left_1": "left",
            "right_1": "right",
            "join_agent_1": "join_agent",
        },
        agent_node_ids=["agent_root_1", "join_agent_1"],
    )


def _dense_workflow(node_count: int, *, agents: bool = False) -> WorkflowInfo:
    node_ids = [f"step_{index}_1" for index in range(node_count)]
    return WorkflowInfo(
        name="dense_agent_captions",
        file_path="dense_agent_captions.py",
        node_ids=node_ids,
        graph={node_ids[index]: node_ids[index + 1 :] for index in range(node_count - 1)},
        node_types={node_id: "step" for node_id in node_ids},
        agent_node_ids=node_ids if agents else [],
    )


@pytest.mark.parametrize("status", list(NodeStatus))
def test_agent_caption_remains_visible_for_every_status_and_selection(status: NodeStatus):
    dag, nodes = workflow_to_layout(_workflow_with_agent_step())
    agent = next(node for node in nodes if node.is_agent)
    lines = render_dag_rich(
        dag,
        {node.name: status for node in nodes},
        frame=1,
        selected=agent,
    )

    assert agent.render_row is not None
    assert agent.caption_render_row == agent.render_row + 1
    assert "(agent)" in lines[agent.caption_render_row].plain
    assert "(agent)" not in lines[agent.render_row].plain
    assert any(span.style.dim for span in lines[agent.caption_render_row].spans)


def test_agent_captions_expand_parallel_rows_without_touching_skip_edges():
    dag, nodes = workflow_to_layout(_workflow_with_parallel_agent_steps())
    assert ("source_1", "join_1") in dag.skip_edges

    lines = render_dag_rich(
        dag,
        {node.name: NodeStatus.PENDING for node in nodes},
        frame=0,
        selected=None,
    )
    for node in (node for node in nodes if node.is_agent):
        assert node.render_row is not None
        assert node.caption_render_row == node.render_row + 1
        primary = lines[node.render_row].plain
        caption = lines[node.caption_render_row].plain
        assert node.display_name in primary
        assert "(agent)" in caption
        caption_col = primary.index(node.display_name)
        assert caption[caption_col : caption_col + len("(agent)")] == "(agent)"
        assert "(agent)" not in primary

    source = next(node for node in nodes if node.name == "source_1")
    assert source.caption_render_row is not None
    connector_row = next(i for i, line in enumerate(lines) if "╰" in line.plain)
    source_drop_col = next(
        col for col, glyph in enumerate(lines[source.caption_render_row].plain) if glyph == "┆"
    )
    assert all(
        lines[row].plain[source_drop_col] == "┆"
        for row in range(source.caption_render_row, connector_row)
    )

    widget = DagWidget()
    widget._test_store = SimpleNamespace(
        dag=dag,
        node_statuses={node.name: NodeStatus.PENDING for node in nodes},
        frame=0,
        selected_node=None,
        node_elapsed={},
        all_nodes=nodes,
    )
    widget.render()
    for node in (node for node in nodes if node.is_agent):
        region = next(
            region
            for region in widget._hit_regions
            if region[3] is node and region[0] == node.caption_render_row
        )
        row, col_start, col_end, _ = region
        assert lines[row].plain[col_start:col_end] == "(agent)"


def test_cross_track_skip_drop_stays_continuous_beside_agent_captions():
    dag, nodes = workflow_to_layout(_workflow_with_cross_track_agent_skip())
    assert len(dag.tracks) > 1
    assert ("agent_root_1", "join_agent_1") in dag.skip_edges

    lines = render_dag_rich(
        dag,
        {node.name: NodeStatus.PENDING for node in nodes},
        frame=0,
        selected=None,
    )
    source = next(node for node in nodes if node.name == "agent_root_1")
    target = next(node for node in nodes if node.name == "join_agent_1")
    assert source.caption_render_row is not None
    assert source.caption_col is not None
    assert target.caption_render_row is not None
    assert target.caption_col is not None
    assert (
        lines[source.caption_render_row].plain[
            source.caption_col : source.caption_col + len("(agent)")
        ]
        == "(agent)"
    )
    assert (
        lines[target.caption_render_row].plain[
            target.caption_col : target.caption_col + len("(agent)")
        ]
        == "(agent)"
    )

    connector_row = next(i for i, line in enumerate(lines) if "╰" in line.plain)
    source_lane = source.caption_col + len("(agent)")
    assert lines[source.caption_render_row].plain[source_lane] == "┆"

    target_lane = target.caption_col + len("(agent)")
    assert lines[target.caption_render_row].plain[target_lane] == "┆"
    assert all(
        lines[row].plain[target_lane] == "┆"
        for row in range(target.caption_render_row + 1, connector_row)
    )


def test_dense_skip_edges_render_without_quadratic_lane_scan():
    workflow = _dense_workflow(25)
    started_at = time.monotonic()
    dag, nodes = workflow_to_layout(workflow)
    lines = render_dag_rich(
        dag,
        {node.name: NodeStatus.PENDING for node in nodes},
        frame=0,
        selected=None,
    )

    assert len(dag.skip_edges) == 276
    assert lines
    assert time.monotonic() - started_at < 5

    small_dag, small_nodes = workflow_to_layout(_dense_workflow(5, agents=True))
    small_lines = render_dag_rich(
        small_dag,
        {node.name: NodeStatus.PENDING for node in small_nodes},
        frame=0,
        selected=None,
    )
    leader_lanes = set()
    for node in small_nodes:
        assert node.render_row is not None
        assert node.caption_render_row is not None
        assert node.caption_col is not None
        lane = node.caption_col - 1
        assert small_lines[node.caption_render_row].plain[lane] == "┆"
        leader_lanes.add(lane)
    assert len(leader_lanes) == len(small_nodes)


@pytest.mark.asyncio
async def test_duplicate_agent_captions_keep_distinct_click_and_scroll_anchors():
    workflow = _workflow_with_duplicate_agent_steps()
    provider = MockStateProvider()
    provider._workflows[workflow.selector] = workflow
    app = AvalancheApp(provider=provider, workflow=workflow.selector)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        dag = app._screen.query_one("#dag-panel")
        dag.render()
        agent_nodes = [node for node in app.store.all_nodes if node.is_agent]
        assert len(agent_nodes) == 2

        caption_regions = {
            node.name: next(
                region
                for region in dag._hit_regions
                if region[3] is node and region[0] == node.caption_render_row
            )
            for node in agent_nodes
        }
        first_caption = caption_regions[agent_nodes[0].name]
        second_caption = caption_regions[agent_nodes[1].name]
        assert first_caption[1] != second_caption[1]

        for node in agent_nodes:
            row, col_start, col_end, _ = caption_regions[node.name]
            await pilot.click("#dag-panel", offset=((col_start + col_end) // 2, row))
            await pilot.pause()
            assert app.store.selected_node is not None
            assert app.store.selected_node.name == node.name
            dag.render()
            assert dag._last_scrolled_node is not None
            assert dag._last_scrolled_node.name == node.name


@pytest.mark.asyncio
async def test_agent_dag_omits_legend_and_preserves_inspector_activation():
    app = AvalancheApp(workflow="agent_trace")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        dag = app._screen.query_one("#dag-panel")
        rendered = dag.render().plain

        assert "Click or" not in rendered
        assert "Enter inspect" not in rendered
        assert "(agent) agent step" not in rendered

        agent_node = next(node for node in app.store.all_nodes if node.is_agent)
        dag.render()  # populate name and caption click regions
        caption_region = next(
            region
            for region in dag._hit_regions
            if region[3] is agent_node and region[0] == agent_node.caption_render_row
        )
        row, col_start, col_end, _ = caption_region
        await pilot.click("#dag-panel", offset=((col_start + col_end) // 2, row))
        await pilot.pause()
        assert app.store.selected_node is not None
        assert app.store.selected_node.name == agent_node.name
        app.select_node(agent_node)
        await pilot.press("enter")
        await pilot.pause()

        assert app.store.trace_inspector_open
