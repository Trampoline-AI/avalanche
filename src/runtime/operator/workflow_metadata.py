"""Stable workflow metadata extraction for operator projections."""

from __future__ import annotations

import inspect
import textwrap

from avalanche.dag import NodeType, Workflow


def step_interface_for_workflow(workflow: Workflow, node_ids: list[str]) -> dict[str, str]:
    """Snapshot each node's Python interface independently of invocation metadata."""
    return {
        node_id: workflow.nodes[node_id].node.step_interface.model_dump_json()
        for node_id in node_ids
    }


def node_docstring_lines_for_workflow(
    workflow: Workflow, node_ids: list[str]
) -> dict[str, str]:
    """Return first docstring lines for source-bearing nodes, including classifiers."""
    lines_by_node: dict[str, str] = {}
    for node_id in node_ids:
        node = workflow.nodes[node_id].node
        if node.node_type not in (NodeType.SOURCE, NodeType.STEP, NodeType.DEST):
            continue
        if getattr(node.fn, "__agent_step__", None) is not None:
            continue
        docstring = inspect.getdoc(node.fn)
        if docstring is None:
            continue
        docstring_line = next(
            (line.strip() for line in docstring.splitlines() if line.strip()), ""
        )
        if docstring_line:
            lines_by_node[node_id] = docstring_line
    return lines_by_node


def node_source_code_for_workflow(workflow: Workflow, node_ids: list[str]) -> dict[str, str]:
    """Return source blocks for ordinary and classifier nodes, excluding agents."""
    source_by_node: dict[str, str] = {}
    for node_id in node_ids:
        node = workflow.nodes[node_id].node
        if node.node_type not in (NodeType.SOURCE, NodeType.STEP, NodeType.DEST):
            continue
        if getattr(node.fn, "__agent_step__", None) is not None:
            continue
        try:
            source_code = textwrap.dedent(inspect.getsource(node.fn)).rstrip()
        except (OSError, TypeError):
            continue
        if source_code:
            source_by_node[node_id] = source_code
    return source_by_node
