"""Qualify a freshly imported visit graph before preparation or execution."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

from avalanche.dag import NodeFuture, Workflow
from avalanche.waits import WaitNode


def qualify_visit(workflow: Workflow, visit: int) -> Workflow:
    graph = deepcopy(workflow)
    prefix = f"v{visit}."
    identities = {node_id: prefix + node_id for node_id in graph.nodes}
    seen: set[int] = set()

    def qualify(value: object) -> None:
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, NodeFuture):
            value.future_id = identities[value.future_id]
            for argument in value.args:
                qualify(argument)
            for argument in value.kwargs.values():
                qualify(argument)
            for incoming in value._incoming_refs:
                qualify(incoming)
            qualify(value.chain_end)
        elif isinstance(value, (list, tuple)):
            for item in value:
                qualify(item)
        elif isinstance(value, dict):
            for item in value.values():
                qualify(item)

    for ref in graph.nodes.values():
        qualify(ref)
    qualify(graph.returns)
    graph.graph = defaultdict(
        list,
        {
            identities[parent]: [identities[child] for child in children]
            for parent, children in graph.graph.items()
        },
    )
    graph.nodes = {identities[node_id]: ref for node_id, ref in graph.nodes.items()}
    for ref in graph.nodes.values():
        ref.graph_ref = graph.graph
    graph.node_slugs = {identities[node_id]: slug for node_id, slug in graph.node_slugs.items()}
    graph.node_waits = {
        identities[node_id]: spec.model_copy(
            update={
                "run_start_node_id": None if spec.run_start_node_id is None
                else identities[spec.run_start_node_id],
                "show_node_id": None
                if spec.show_node_id is None
                else identities[spec.show_node_id],
                "map_source_node_id": None
                if spec.map_source_node_id is None
                else identities[spec.map_source_node_id],
            }
        )
        for node_id, spec in graph.node_waits.items()
    }
    for node_id, spec in graph.node_waits.items():
        node = graph.nodes[node_id].node
        if not isinstance(node, WaitNode):
            raise TypeError("Wait declaration belongs to a non-wait node")
        node.spec = spec
    graph.node_runs = {identities[node_id]: spec for node_id, spec in graph.node_runs.items()}
    graph.node_machines = {
        identities[node_id]: spec for node_id, spec in graph.node_machines.items()
    }
    return graph
