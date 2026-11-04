"""Dynamic local ready queue. Map groups are bindings; only real items execute."""

from __future__ import annotations

import os
from concurrent.futures import FIRST_COMPLETED, CancelledError, Future, ThreadPoolExecutor, wait
from contextvars import copy_context
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


def run_dynamic[T](
    *,
    run_id: str,
    order: list[str],
    nodes: dict[str, NodeFuture],
    node_slugs: dict[str, str],
    dependencies: dict[str, list[str]],
    execute: Callable[[str], None],
    resolve_list: Callable[[list[T] | NodeFuture | InputRef], list[T]],
    gather: Callable[[str, tuple[str, ...]], None],
    max_workers: int | None,
    hooks: RunHooks | None,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    groups: dict[str, _Group[T]] = {
        node_id: _Group(future)
        for node_id, future in nodes.items()
        if isinstance(future, MapFuture)
    }
    expected_counts: dict[str, int] = {}
    completed: set[str] = set()
    failed: set[str] = set()
    ready = [node_id for node_id in order if node_id not in groups]
    children: dict[str, tuple[str, int]] = {}
    pending: dict[Future[None], str] = {}
    primary_failure: BaseException | None = None
    capacity = (
        max_workers if max_workers is not None else min(32, (os.process_cpu_count() or 1) + 4)
    )
    pool = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix=f"avalanche-{run_id}")

    def emit_failure(node_id: str, error: Exception) -> None:
        failed.add(node_id)
        if hooks and hooks.on_node_failure:
            hooks.on_node_failure(node_id, error)

    def publish(parent: str, ids: tuple[str, ...], n: int) -> None:
        chain = {}
        frontier = [parent]
        while frontier:
            source = frontier.pop()
            for target, group in groups.items():
                if target not in chain and any(
                    isinstance(arg, MapFuture) and arg.future_id == source
                    for arg in group.future.args
                ):
                    chain[target] = n
                    frontier.append(target)
        for target, count in {parent: n, **chain}.items():
            if target in expected_counts and expected_counts[target] != count:
                error = ValueError(
                    f"{groups[target].future.node.name}: map_length_mismatch "
                    f"lengths {[expected_counts[target], count]}"
                )
                emit_failure(target, error)
                raise error
        expected_counts.update({parent: n, **chain})
        if hooks and hooks.on_nodes_expanded:
            hooks.on_nodes_expanded(
                NodesExpanded(
                    run_id=run_id,
                    parent=parent,
                    children=ids,
                    n_expected=n,
                    chain=chain,
                )
            )

    def expand() -> bool:
        changed = False
        for parent, group in groups.items():
            if group.finished:
                continue
            future = group.future
            mapped_sources = {
                arg.future_id for arg in future.args if isinstance(arg, MapFuture)
            }
            plain_dependencies = set(dependencies.get(parent, ())) - mapped_sources
            if not plain_dependencies <= completed:
                continue
            if any(groups[source].n is None for source in mapped_sources):
                continue
            if group.n is None:
                lengths = []
                for position, arg in enumerate(future.args):
                    if isinstance(arg, MapFuture):
                        lengths.append(groups[arg.future_id].n)
                    else:
                        group.plain[position] = resolve_list(arg)
                        lengths.append(len(group.plain[position]))
                if len(set(lengths)) != 1:
                    error = ValueError(
                        f"{future.node.name}: map_length_mismatch lengths {lengths}"
                    )
                    emit_failure(parent, error)
                    raise error
                n = lengths[0]
                assert n is not None
                if n > future.map_spec.max_items:
                    error = ValueError(
                        f"{future.node.name}: map_overflow input length {n} exceeds max_items "
                        f"{future.map_spec.max_items} (raise with max_items=, hard cap 64)"
                    )
                    emit_failure(parent, error)
                    raise error
                group.n = n
                publish(parent, (), n)
                if hooks and hooks.on_node_start:
                    hooks.on_node_start(parent)
                changed = True
            n = group.n
            assert n is not None
            ids = []
            for index in range(n):
                if index in group.created:
                    continue
                upstream_items = {f"{source}[{index}]" for source in mapped_sources}
                if not upstream_items <= completed:
                    continue
                child_id = f"{parent}[{index}]"
                args = tuple(
                    nodes[f"{arg.future_id}[{index}]"]
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
                nodes[child_id] = child
                node_slugs[child_id] = child.node_slug
                dependencies[child_id] = [*plain_dependencies, *sorted(upstream_items)]
                group.created.add(index)
                children[child_id] = (parent, index)
                ids.append(child_id)
            if ids:
                if mapped_sources:
                    ready[0:0] = ids
                else:
                    ready.extend(ids)
                publish(parent, tuple(ids), n)
                changed = True
            item_ids = tuple(f"{parent}[{i}]" for i in range(n))
            if all(child_id in completed for child_id in item_ids):
                gather(parent, item_ids)
                group.finished = True
                completed.add(parent)
                if hooks and hooks.on_node_success:
                    hooks.on_node_success(parent)
                changed = True
            elif all(child_id in completed or child_id in failed for child_id in item_ids):
                error = MapItemsFailed(future.node.name, tuple(group.failures))
                emit_failure(parent, error)
                group.finished = True
                changed = True
        return changed

    try:
        while True:
            if cancel_requested and cancel_requested():
                raise CancelledError()
            while expand():
                pass
            for node_id in list(ready):
                if len(pending) >= capacity:
                    break
                if not set(dependencies.get(node_id, ())) <= completed:
                    continue
                if hooks and hooks.on_node_start:
                    hooks.on_node_start(node_id)
                pending[pool.submit(copy_context().run, execute, node_id)] = node_id
                ready.remove(node_id)
            if not pending:
                break
            wait(tuple(pending), return_when=FIRST_COMPLETED)
            for task in tuple(pending):
                if not task.done():
                    continue
                node_id = pending.pop(task)
                try:
                    task.result()
                except Exception as error:
                    failed.add(node_id)  # execute publishes the ordinary item failure.
                    if node_id not in children:
                        raise
                    parent, index = children[node_id]
                    group = groups[parent]
                    group.failures.append(MapItemFailure(index, node_id, error))
                    if group.future.map_spec.fail_fast:
                        failure = MapItemsFailed(group.future.node.name, tuple(group.failures))
                        emit_failure(parent, failure)
                        raise failure from error
                else:
                    completed.add(node_id)
        failures = tuple(item for group in groups.values() for item in group.failures)
        if failures:
            raise MapItemsFailed("workflow", failures)
        if ready or any(not group.finished for group in groups.values()):
            raise RuntimeError("Local workflow scheduler has no runnable nodes")
    except BaseException as error:
        primary_failure = error
        # Local synchronous Python calls cannot be interrupted safely. Cancel queued
        # futures and stop admission; already-running calls finish before return.
        for task in pending:
            task.cancel()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=primary_failure is not None)
