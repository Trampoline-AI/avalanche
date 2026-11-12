"""Declared run operations. The scheduler, never a tenant HTTP client, starts runs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from .dag import Node, NodeFuture, NodeType, Workflow, _workflow_context
from .policies import NonRetryableError, StepPolicy, WaitSpec
from .results import RunResult
from .runtime import BaseInput
from .waits import WaitNode, _seconds


class RunStart(BaseModel):
    """Normal run-start outcome, including permanent keyed-run tombstones."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)
    run_id: str
    key: list[JsonValue] | None = None
    created: bool
    read_state: Literal["retained", "expired"] = "retained"


class RunOpSpec(BaseModel):
    """Frozen scheduler metadata; target, input and when are normal node bindings."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    op: Literal["start"] = "start"


# Machine implements the same zero-argument workflow-builder protocol.
# Keep this alias evaluable without introducing the machine/dag import cycle.
type RunTarget = Workflow | Callable[[], Workflow]


class RunNode(Node):
    _expand_single_return = False

    def __init__(self, definition: RunTarget | None) -> None:
        def operation(
            target: str,
            input: BaseInput | dict[str, JsonValue] | None = None,
            *,
            when: bool = True,
        ) -> RunStart | None:
            raise RuntimeError("RUN start requires a run-capable scheduler host")

        operation.__name__ = "start_run"
        super().__init__(operation, NodeType.RUN, policy=StepPolicy(retries=0))
        self.spec = RunOpSpec()
        self.definition = definition


def start_run(
    target: RunTarget | str | NodeFuture,
    input: BaseInput | dict[str, JsonValue] | NodeFuture | None = None,
    *,
    when: bool | NodeFuture = True,
) -> NodeFuture:
    """Declare a flow or machine run start, with optional runtime conditional skip.

    ``when=False`` completes the node with None, without validating other arguments
    or contacting the host. It satisfies ordering edges; it does not skip children.
    A runtime target resolves to a catalog name. Tests register dynamic targets via
    ``workflow.test(targets=(...))``. Outside workflows use the host's normal run client.
    """
    if _workflow_context.get() is None:
        raise TypeError("ava.start_run is workflow-only; use ava run or a normal run client")
    if isinstance(target, (str, NodeFuture)):
        name, definition = target, None
    else:
        definition = target
        name = target.name if isinstance(target, Workflow) else target.__name__
    return RunNode(definition)(name, input, when=when)


class ChildRunError(NonRetryableError):
    def __init__(self, code: str, run_id: str) -> None:
        self.code = code
        self.run_id = run_id
        super().__init__(f"{code}: {run_id}")


def check_run_wait(parent: str, child: str, waiting: dict[str, set[str]]) -> None:
    """Refuse a new edge if its target can already reach the caller."""
    if parent == child:
        raise ChildRunError("child_run_self_wait", child)
    pending, seen = [child], set()
    while pending:
        current = pending.pop()
        if current == parent:
            raise ChildRunError("child_run_wait_cycle", child)
        if current not in seen:
            seen.add(current)
            pending.extend(waiting.get(current, ()))


class RunWaitNode(WaitNode):
    def __init__(self, spec: WaitSpec, definition: RunTarget | None) -> None:
        super().__init__(spec, None)
        self.definition = definition

        def call_result(start: RunStart | None) -> RunResult | None:
            raise RuntimeError("Run waits require a run-capable scheduler host")

        call_result.__name__ = "call"
        self.fn = call_result

    def decode(self, result: RunResult | None):
        if result is None:
            return None
        value = result.decode()
        if self.definition is None:
            return value
        from .machine import Machine

        if isinstance(self.definition, Machine):
            from functools import reduce
            from operator import or_

            self.definition()
            import inspect

            models = []
            for state in self.definition.spec.states.values():
                if not state.final:
                    continue
                work = self.definition.works[state.work]
                model = work.output_type
                if model is None and isinstance(work.returns, NodeFuture):
                    model = inspect.signature(
                        work.returns.node.fn, eval_str=True
                    ).return_annotation
                if model is not None and model is not inspect.Signature.empty:
                    models.append(model)
            return TypeAdapter(reduce(or_, models)).validate_python(value) if models else value
        flow = self.definition if isinstance(self.definition, Workflow) else self.definition()
        if flow.output_type is not None:
            return flow.validate_output(value)
        if isinstance(flow.returns, NodeFuture):
            import inspect

            signature = inspect.signature(flow.returns.node.fn, eval_str=True)
            annotation = signature.return_annotation
            if annotation is not inspect.Signature.empty:
                return TypeAdapter(annotation).validate_python(value)
        return value


def call(
    target: RunTarget | str | NodeFuture,
    input: BaseInput | dict[str, JsonValue] | NodeFuture | None = None,
    *,
    timeout: timedelta | float | None = None,
    when: bool | NodeFuture = True,
) -> NodeFuture:
    """Declare a child start and workerless wait for its final typed output.

    Timeout does not cancel the child. Existing keys attach to the original run.
    Machine calls resolve only when the instance closes, not after a state entry.
    """
    spec = WaitSpec(
        kind="run", timeout_seconds=_seconds(timeout) if timeout is not None else None
    )
    started = start_run(target, input, when=when)
    spec = spec.model_copy(update={"run_start_node_id": started.future_id})
    result = RunWaitNode(spec, started.node.definition)(started)
    result.chain_start = started
    return result


def operation_enabled(value: JsonValue) -> bool:
    """Strict graph skip rule shared by declared starts and sends."""
    return TypeAdapter(bool).validate_python(value, strict=True)
