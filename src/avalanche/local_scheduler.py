"""Embedded dependency scheduler: only executable nodes enter the worker pool."""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextvars import copy_context
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, JsonValue, TypeAdapter

from .input_ref import InputRef
from .local_waits import LocalWaits, Reconcile
from .policies import MAX_GENERATION, NonRetryableError, OutcomeUnknownError
from .waits import WaitNode

if TYPE_CHECKING:
    from runtime.operator.hooks import RunHooks

    from .dag import NodeFuture, Workflow

T = TypeVar("T")


def run_local_schedule(
    workflow: Workflow,
    order: list[str],
    dependencies: dict[str, list[str]],
    results: dict[str, T],
    max_workers: int | None,
    waits: LocalWaits,
    submit: Callable[..., tuple[NodeFuture, T]],
    complete: Callable[[str, NodeFuture, T], None],
    hooks: RunHooks | None,
    cancel_requested: Callable[[], bool] | None,
    resolve_list: Callable[[list[T] | NodeFuture | InputRef], list[T]],
    gather: Callable[[str, tuple[str, ...]], None],
    submit_machine: Callable[[str], Future[tuple[NodeFuture, T]]],
) -> None:
    capacity = max_workers if max_workers is not None else min(32, (os.cpu_count() or 1) + 4)
    remaining = list(order)
    completed: set[str] = set()
    failed: set[str] = set()
    pending: dict[Future[tuple[NodeFuture, T]], str] = {}
    parked: set[str] = set()
    holds: set[str] = set()
    map_groups: dict[str, tuple[str, ...]] = {}
    show_decisions: dict[str, str] = {}
    attempts: dict[str, int] = dict.fromkeys(order, 0)
    failure: Exception | None = None
    cancelled = False

    from runtime.operator.map_driver import DynamicMaps

    maps = DynamicMaps(
        run_id=waits.run_id,
        nodes=workflow.nodes,
        node_slugs=workflow.node_slugs,
        dependencies=dependencies,
        ready=remaining,
        completed=completed,
        failed=failed,
        resolve_list=resolve_list,
        gather=gather,
        hooks=hooks,
    )

    def notify(_future: Future[tuple[NodeFuture, T]]) -> None:
        waits.notify()

    def reviewed(node_id: str) -> JsonValue:
        from .dag import _unwrap_lineaged_tree

        spec = workflow.node_waits[node_id]
        if spec.show_node_id is None:
            return spec.show
        value = _unwrap_lineaged_tree(results[spec.show_node_id])
        if spec.show_index is not None:
            value = value[spec.show_index]
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        return TypeAdapter(JsonValue).validate_python(value)

    with ThreadPoolExecutor(
        max_workers=capacity, thread_name_prefix=f"avalanche-local-{waits.run_id}"
    ) as pool:
        while (
            remaining
            or pending
            or parked
            or map_groups
            or any(not group.finished for group in maps.groups.values())
        ):
            with waits.condition:
                if (
                    (remaining or parked or map_groups)
                    and cancel_requested is not None
                    and cancel_requested()
                ):
                    cancelled = True
                if failure is not None or cancelled:
                    break
                progressed = False
                for task in list(pending):
                    if not task.done():
                        continue
                    node_id = pending.pop(task)
                    progressed = True
                    ref = workflow.nodes[node_id]
                    try:
                        node_ref, value = task.result()
                    except Exception as exc:
                        policy = ref.node.policy
                        if (
                            isinstance(exc, OutcomeUnknownError)
                            and policy.on_uncertain == "hold"
                        ):
                            annotation = inspect.signature(ref.node.fn).return_annotation
                            if annotation is inspect.Signature.empty:
                                failure = TypeError("hold steps require a return annotation")
                                break
                            waits.add_hold(node_id, TypeAdapter(annotation))
                            waits.arm(node_id)
                            holds.add(node_id)
                            parked.add(node_id)
                        elif (
                            not isinstance(exc, NonRetryableError)
                            and not (
                                isinstance(exc, OutcomeUnknownError)
                                and policy.on_uncertain == "fail"
                            )
                            and attempts[node_id] <= policy.effective_retries
                        ):
                            remaining.append(node_id)
                        else:
                            if hooks and hooks.on_node_failure:
                                hooks.on_node_failure(node_id, exc)
                                if node_id in show_decisions:
                                    hooks.on_node_failure(show_decisions[node_id], exc)
                            if node_id in show_decisions or not maps.item_failed(node_id, exc):
                                failure = exc
                                break
                    else:
                        complete(node_id, node_ref, value)
                        completed.add(node_id)
                        if node_id in show_decisions:
                            from .dag import _unwrap_lineaged_tree

                            shown = _unwrap_lineaged_tree(results[node_id])
                            if not isinstance(shown, BaseModel):
                                raise TypeError(
                                    "decision.map show must return a Pydantic model"
                                )
                            child = show_decisions[node_id]
                            if hooks and hooks.on_node_start:
                                hooks.on_node_start(child)
                            waits.arm_shown_decision(child, shown.model_dump(mode="json"))

                for group, children in list(map_groups.items()):
                    if not all(waits.resolved(child) for child in children):
                        continue
                    try:
                        outputs = [waits.result(child) for child in children]
                        complete(group, workflow.nodes[group], outputs)
                    except Exception as exc:
                        failure = exc
                        if hooks and hooks.on_node_failure:
                            hooks.on_node_failure(group, exc)
                        break
                    completed.add(group)
                    del map_groups[group]
                    progressed = True

                for node_id in list(parked):
                    if not waits.resolved(node_id):
                        continue
                    progressed = True
                    try:
                        value = waits.result(node_id)
                        if node_id in holds:
                            if not isinstance(value, Reconcile):
                                raise TypeError("hold requires a reconciliation outcome")
                            if value.kind == "retry":
                                if attempts[node_id] >= MAX_GENERATION:
                                    raise OutcomeUnknownError("generation_ceiling")
                                # Human authorization is one new attempt, not a reset of
                                # the automatic ladder or of the logical step key.
                                remaining.append(node_id)
                                holds.remove(node_id)
                                parked.remove(node_id)
                                continue
                            if value.kind == "fail":
                                raise OutcomeUnknownError("operator failed uncertain step")
                            annotation = inspect.signature(
                                workflow.nodes[node_id].node.fn
                            ).return_annotation
                            value = TypeAdapter(annotation).validate_python(value.output)
                            holds.remove(node_id)
                        complete(node_id, workflow.nodes[node_id], value)
                    except Exception as exc:
                        if hooks and hooks.on_node_failure:
                            hooks.on_node_failure(node_id, exc)
                        parked.remove(node_id)
                        holds.discard(node_id)
                        if node_id in show_decisions or not maps.item_failed(node_id, exc):
                            failure = exc
                            break
                        continue
                    completed.add(node_id)
                    parked.remove(node_id)

                if failure is not None:
                    break
                while maps.expand():
                    progressed = True
                for node_id in list(remaining):
                    if cancel_requested is not None and cancel_requested():
                        cancelled = True
                        break
                    ref = workflow.nodes[node_id]
                    node = ref.node
                    mapped = isinstance(node, WaitNode) and node.spec.map_max_items is not None
                    show_group_id = node.spec.show_node_id if mapped else None
                    if not all(
                        parent in completed
                        for parent in dependencies.get(node_id, [])
                        if parent != show_group_id
                    ):
                        continue
                    if isinstance(ref.node, WaitNode):
                        if hooks and hooks.on_node_start:
                            hooks.on_node_start(node_id)
                        spec = ref.node.spec
                        if spec.map_max_items is not None:
                            from .dag import _unwrap_lineaged_tree

                            if spec.map_source_node_id is not None:
                                source = ref.kwargs["items"]
                                items = _unwrap_lineaged_tree(results[source.future_id])
                                if source.tuple_index is not None:
                                    items = items[source.tuple_index]
                                if not isinstance(items, list):
                                    raise TypeError("decision.map input must resolve to a list")
                                wire = [
                                    item.model_dump(mode="json")
                                    if isinstance(item, BaseModel)
                                    else item
                                    for item in items
                                ]
                            else:
                                assert spec.map_items is not None
                                wire = spec.map_items
                                items = ref.node.map_values
                            validated = TypeAdapter(list[JsonValue]).validate_python(wire)
                            children = waits.expand_decisions(
                                node_id, validated, arm=show_group_id is None
                            )
                            map_groups[node_id] = children
                            if show_group_id is not None:
                                n = len(children)
                                show_ids = [f"{show_group_id}[{i}]" for i in range(n)]
                                dependencies[node_id] = show_ids
                                show_decisions.update(zip(show_ids, children, strict=True))
                        else:
                            waits.arm(node_id, reviewed(node_id))
                            parked.add(node_id)
                    elif node.node_type.value in {"machine", "run"}:
                        if hooks and hooks.on_node_start:
                            hooks.on_node_start(node_id)
                        attempts[node_id] = attempts.get(node_id, 0) + 1
                        task = submit_machine(node_id)
                        pending[task] = node_id
                        task.add_done_callback(notify)
                    else:
                        if len(pending) >= capacity:
                            continue
                        if hooks and hooks.on_node_start:
                            hooks.on_node_start(node_id)
                        attempts[node_id] = attempts.get(node_id, 0) + 1
                        task = pool.submit(
                            copy_context().run, submit, node_id, emit_hooks=False
                        )
                        pending[task] = node_id
                        task.add_done_callback(notify)
                    remaining.remove(node_id)
                    progressed = True
                if cancelled:
                    break
                if not progressed and not pending and not parked and not map_groups:
                    maps.raise_failures()
                    if remaining or any(not group.finished for group in maps.groups.values()):
                        raise RuntimeError("Local workflow scheduler has no runnable nodes")
                if not progressed and (remaining or pending or parked or map_groups):
                    waits.condition.wait(timeout=waits.next_deadline())

    if failure is not None:
        raise failure
    if cancelled:
        raise CancelledError()
    maps.raise_failures()
