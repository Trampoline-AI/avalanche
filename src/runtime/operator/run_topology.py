"""Apply validated map publication to one run, without changing the deployment."""

from dataclasses import replace

from avalanche.mapping import NodesExpanded

from .models import NodeState, RunState


def expand_run(run: RunState, expansion: NodesExpanded) -> tuple[str, ...]:
    specs = dict(run.topology.map_specs)
    parent = expansion.parent
    if expansion.run_id != run.run_id or parent not in specs:
        raise ValueError("expansion must reference a declared map in this run")
    counts = {parent: expansion.n_expected, **expansion.chain}
    if any(node_id not in specs or n != expansion.n_expected for node_id, n in counts.items()):
        raise ValueError("expansion chain must reference declared maps with the same length")
    for node_id, n in counts.items():
        if node_id in run.expansions and run.expansions[node_id] != n:
            raise ValueError("expansion changed an established map length")
    indices = []
    for child in expansion.children:
        if child in run.nodes or not child.startswith(f"{parent}[") or not child.endswith("]"):
            raise ValueError("expansion contains duplicate or invalid child identity")
        index = int(child[len(parent) + 1 : -1])
        if not 0 <= index < expansion.n_expected or child != f"{parent}[{index}]":
            raise ValueError("expansion child index is outside the map")
        indices.append(index)
    if len(set(indices)) != len(indices):
        raise ValueError("expansion contains duplicate indices")
    template = run.nodes[parent]
    for child, index in zip(expansion.children, indices, strict=True):
        run.nodes[child] = NodeState(
            node_id=child,
            name=f"{template.name}[{index}]",
            node_type=template.node_type,
            map_parent=parent,
            map_index=index,
        )
    topology = run.topology

    def inherit(pairs: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        return pairs + tuple(
            (child, value)
            for key, value in pairs
            if key == parent
            for child in expansion.children
        )

    run.topology = replace(
        topology,
        node_ids=topology.node_ids + expansion.children,
        graph=topology.graph + tuple((child, ()) for child in expansion.children),
        node_types=inherit(topology.node_types),
        display_names=topology.display_names
        + tuple((child, run.nodes[child].name) for child in expansion.children),
        agent_field_schemas_json=inherit(topology.agent_field_schemas_json),
        agent_instruction_lines=inherit(topology.agent_instruction_lines),
        agent_runtime_kinds=inherit(topology.agent_runtime_kinds),
        standard_step_docstring_lines=inherit(topology.standard_step_docstring_lines),
        classifier_metadata_json=inherit(topology.classifier_metadata_json),
        evaluation_metadata_json=inherit(topology.evaluation_metadata_json),
        step_interface_json=inherit(topology.step_interface_json),
    )
    run.expansions = {**run.expansions, **counts}
    return expansion.children
