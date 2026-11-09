"""Declared run operations. The scheduler, never a tenant HTTP client, starts runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from .dag import Node, NodeFuture, NodeType, Workflow, _workflow_context
from .policies import StepPolicy
from .runtime import BaseInput

if TYPE_CHECKING:
    from .machine import Machine


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


type RunTarget = Machine | Workflow | Callable[[], Workflow]


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
        graph = target if isinstance(target, Workflow) else target()
        name = graph.name
    return RunNode(definition)(name, input, when=when)


def operation_enabled(value: JsonValue) -> bool:
    """Strict graph skip rule shared by declared starts and sends."""
    return TypeAdapter(bool).validate_python(value, strict=True)
