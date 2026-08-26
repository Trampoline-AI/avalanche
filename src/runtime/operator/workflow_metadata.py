"""Stable workflow metadata extraction for operator projections."""

from __future__ import annotations

import inspect

from avalanche.dag import NodeType, Workflow


def standard_step_docstring_lines_for_workflow(
    workflow: Workflow, node_ids: list[str]
) -> dict[str, str]:
    """Return first non-empty docstring lines for non-agent standard steps."""
    lines_by_node: dict[str, str] = {}
    for node_id in node_ids:
        node = workflow.nodes[node_id].node
        if node.node_type is not NodeType.STEP:
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
