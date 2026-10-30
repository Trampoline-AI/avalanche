"""Stable workflow metadata extraction for operator projections."""

from __future__ import annotations

import inspect
import textwrap

from pydantic import BaseModel, ConfigDict, JsonValue

from avalanche.agent.declaration import AgentDeclaration
from avalanche.dag import NodeFuture, NodeType, Workflow, run_input_parameters
from avalanche.input_ref import InputRef
from avalanche.runtime import BaseInput


class WorkflowInputDeclaration(BaseModel):
    """The declared run input model and the steps that receive it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type_name: str
    json_schema: dict[str, JsonValue]
    consumer_node_ids: tuple[str, ...]


# Run input has no size limit of its own; larger values are run but not displayed.
MAX_PREPARED_INPUT_LENGTH = 1024 * 1024


class PreparedRunInput(BaseModel):
    """The validated values a run starts with and the fields its caller supplied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    values_json: str
    supplied_fields: tuple[str, ...]


def _receives_run_input(future: NodeFuture, input_type: type[BaseInput]) -> bool:
    if any(isinstance(value, InputRef) for value in (*future.args, *future.kwargs.values())):
        return True
    # The same rule the runtime uses to inject the run input into a step.
    return bool(run_input_parameters(future.node.fn, future.args, future.kwargs, input_type))


def input_declaration_for_workflow(workflow: Workflow, node_ids: list[str]) -> str:
    """Describe the declared run input; empty when the workflow declares none."""
    if workflow.input_type is None:
        return ""
    return WorkflowInputDeclaration(
        type_name=workflow.input_type.__name__,
        json_schema=workflow.input_type.model_json_schema(by_alias=False),
        consumer_node_ids=tuple(
            node_id
            for node_id in node_ids
            if _receives_run_input(workflow.nodes[node_id], workflow.input_type)
        ),
    ).model_dump_json()


def agent_declarations_for_workflow(
    workflow: Workflow, node_ids: list[str]
) -> dict[str, AgentDeclaration]:
    """Describe every agent step; a step that cannot be described fails discovery."""
    declarations: dict[str, AgentDeclaration] = {}
    for node_id in node_ids:
        spec = getattr(workflow.nodes[node_id].node.fn, "__agent_step__", None)
        if spec is not None:
            declarations[node_id] = spec.declaration(workflow.agent_defaults)
    return declarations


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
