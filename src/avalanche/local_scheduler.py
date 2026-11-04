"""Embedded dependency scheduler: only executable nodes enter the worker pool."""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextvars import copy_context
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, JsonValue, TypeAdapter

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
) -> None:
    capacity = max_workers if max_workers is not None else min(32, (os.cpu_count() or 1) + 4)
    remaining = list(order)
    completed: set[str] = set()
    pending: dict[Future[tuple[NodeFuture, T]], str] = {}
    parked: set[str] = set()
    holds: set[str] = set()
    attempts: dict[str, int] = dict.fromkeys(order, 0)
    failure: Exception | None = None
    cancelled = False

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
        while remaining or pending or parked:
            with waits.condition:
                if (
                    (remaining or parked)
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
                            failure = exc
                            if hooks and hooks.on_node_failure:
                                hooks.on_node_failure(node_id, exc)
                            break
                    else:
                        complete(node_id, node_ref, value)
                        completed.add(node_id)

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
                        failure = exc
                        if hooks and hooks.on_node_failure:
                            hooks.on_node_failure(node_id, exc)
                        break
                    completed.add(node_id)
                    parked.remove(node_id)

                if failure is not None:
                    break
                for node_id in list(remaining):
                    if cancel_requested is not None and cancel_requested():
                        cancelled = True
                        break
                    if not all(parent in completed for parent in dependencies.get(node_id, [])):
                        continue
                    ref = workflow.nodes[node_id]
                    if isinstance(ref.node, WaitNode):
                        if hooks and hooks.on_node_start:
                            hooks.on_node_start(node_id)
                        waits.arm(node_id, reviewed(node_id))
                        parked.add(node_id)
                    else:
                        if len(pending) >= capacity:
                            continue
                        if hooks and hooks.on_node_start:
                            hooks.on_node_start(node_id)
                        attempts[node_id] += 1
                        task = pool.submit(
                            copy_context().run, submit, node_id, emit_hooks=False
                        )
                        pending[task] = node_id
                        task.add_done_callback(notify)
                    remaining.remove(node_id)
                    progressed = True
                if cancelled:
                    break
                if not progressed and (remaining or pending or parked):
                    waits.condition.wait(timeout=waits.next_deadline())

    if failure is not None:
        raise failure
    if cancelled:
        raise CancelledError()
