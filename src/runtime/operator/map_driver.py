"""Run-local map expansion shared by the ordinary retry/wait scheduler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from avalanche.dag import MapFuture, NodeFuture
from avalanche.input_ref import InputRef
from avalanche.mapping import MapItemFailure, MapItemsFailed, NodesExpanded

from .hooks import RunHooks


@dataclass
class _Group[T]:
    future: MapFuture
    n: int | None = None
    created: set[int] = field(default_factory=set)
    failures: list[MapItemFailure] = field(default_factory=list)
    finished: bool = False
    plain: dict[int, list[T]] = field(default_factory=dict)


class DynamicMaps[T]:
    def __init__(
        self,
        *,
        run_id: str,
        nodes: dict[str, NodeFuture],
        node_slugs: dict[str, str],
        dependencies: dict[str, list[str]],
        ready: list[str],
        completed: set[str],
        failed: set[str],
        resolve_list: Callable[[list[T] | NodeFuture | InputRef], list[T]],
        gather: Callable[[str, tuple[str, ...]], None],
        hooks: RunHooks | None,
    ) -> None:
        self.run_id = run_id
        self.nodes = nodes
        self.node_slugs = node_slugs
        self.dependencies = dependencies
        self.ready = ready
        self.completed = completed
        self.failed = failed
        self.resolve_list = resolve_list
        self.gather = gather
        self.hooks = hooks
        self.groups: dict[str, _Group[T]] = {}
        self.expected_counts: dict[str, int] = {}
        self.children: dict[str, tuple[str, int]] = {}
        for future in tuple(nodes.values()):
            if isinstance(future, MapFuture):
                self.add(future)

    def add(self, future: MapFuture) -> None:
        self.groups[future.future_id] = _Group(future)
        if future.future_id in self.ready:
            self.ready.remove(future.future_id)

    def emit_failure(self, node_id: str, error: Exception) -> None:
        self.failed.add(node_id)
        if self.hooks and self.hooks.on_node_failure:
            self.hooks.on_node_failure(node_id, error)

    def publish(self, parent: str, ids: tuple[str, ...], n: int) -> None:
        chain = {}
        frontier = [parent]
        while frontier:
            source = frontier.pop()
            for target, group in self.groups.items():
                if target not in chain and any(
                    isinstance(arg, MapFuture) and arg.future_id == source
                    for arg in group.future.args
                ):
                    chain[target] = n
                    frontier.append(target)
        for target, count in {parent: n, **chain}.items():
            if target in self.expected_counts and self.expected_counts[target] != count:
                error = ValueError(
                    f"{self.groups[target].future.node.name}: map_length_mismatch "
                    f"lengths {[self.expected_counts[target], count]}"
                )
                self.emit_failure(target, error)
                raise error
        self.expected_counts.update({parent: n, **chain})
        if self.hooks and self.hooks.on_nodes_expanded:
            self.hooks.on_nodes_expanded(
                NodesExpanded(
                    run_id=self.run_id, parent=parent, children=ids, n_expected=n, chain=chain
                )
            )

    def expand(self) -> bool:
        changed = False
        for parent, group in self.groups.items():
            if group.finished:
                continue
            future = group.future
            mapped_sources = {
                arg.future_id for arg in future.args if isinstance(arg, MapFuture)
            }
            plain_dependencies = set(self.dependencies.get(parent, ())) - mapped_sources
            if not plain_dependencies <= self.completed:
                continue
            if any(self.groups[source].n is None for source in mapped_sources):
                continue
            if group.n is None:
                lengths = []
                for position, arg in enumerate(future.args):
                    if isinstance(arg, MapFuture):
                        lengths.append(self.groups[arg.future_id].n)
                    else:
                        group.plain[position] = self.resolve_list(arg)
                        lengths.append(len(group.plain[position]))
                if len(set(lengths)) != 1:
                    error = ValueError(
                        f"{future.node.name}: map_length_mismatch lengths {lengths}"
                    )
                    self.emit_failure(parent, error)
                    raise error
                n = lengths[0]
                assert n is not None
                if n > future.map_spec.max_items:
                    error = ValueError(
                        f"{future.node.name}: map_overflow input length {n} exceeds max_items "
                        f"{future.map_spec.max_items} (raise with max_items=, hard cap 64)"
                    )
                    self.emit_failure(parent, error)
                    raise error
                group.n = n
                self.publish(parent, (), n)
                if self.hooks and self.hooks.on_node_start:
                    self.hooks.on_node_start(parent)
                changed = True
            n = group.n
            assert n is not None
            ids = []
            for index in range(n):
                if index in group.created:
                    continue
                upstream_items = {f"{source}[{index}]" for source in mapped_sources}
                if not upstream_items <= self.completed:
                    continue
                child_id = f"{parent}[{index}]"
                args = tuple(
                    self.nodes[f"{arg.future_id}[{index}]"]
                    if isinstance(arg, MapFuture)
                    else group.plain[position][index]
                    for position, arg in enumerate(future.args)
                )
                child = NodeFuture(
                    node=future.node,
                    future_id=child_id,
                    graph_ref=future.graph_ref,
                    node_slug=f"{future.node_slug}[{index}]",
                    args=args,
                    kwargs=future.kwargs,
                )
                self.nodes[child_id] = child
                self.node_slugs[child_id] = child.node_slug
                self.dependencies[child_id] = [*plain_dependencies, *sorted(upstream_items)]
                group.created.add(index)
                self.children[child_id] = (parent, index)
                ids.append(child_id)
            if ids:
                if mapped_sources:
                    self.ready[0:0] = ids
                else:
                    self.ready.extend(ids)
                self.publish(parent, tuple(ids), n)
                changed = True
            item_ids = tuple(f"{parent}[{index}]" for index in range(n))
            if all(child_id in self.completed for child_id in item_ids):
                self.gather(parent, item_ids)
                group.finished = True
                self.completed.add(parent)
                if self.hooks and self.hooks.on_node_success:
                    self.hooks.on_node_success(parent)
                changed = True
            elif all(
                child_id in self.completed or child_id in self.failed for child_id in item_ids
            ):
                self.emit_failure(
                    parent, MapItemsFailed(future.node.name, tuple(group.failures))
                )
                group.finished = True
                changed = True
        return changed

    def item_failed(self, node_id: str, error: Exception) -> bool:
        if node_id not in self.children:
            return False
        self.failed.add(node_id)
        parent, index = self.children[node_id]
        group = self.groups[parent]
        group.failures.append(MapItemFailure(index, node_id, error))
        if group.future.map_spec.fail_fast:
            failure = MapItemsFailed(group.future.node.name, tuple(group.failures))
            self.emit_failure(parent, failure)
            raise failure from error
        return True

    def raise_failures(self) -> None:
        failures = tuple(item for group in self.groups.values() for item in group.failures)
        if failures:
            raise MapItemsFailed("workflow", failures)
