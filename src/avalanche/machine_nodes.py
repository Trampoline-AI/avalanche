"""Declared machine steps; only a machine-capable host may execute them."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, JsonValue, TypeAdapter

from .dag import Node, NodeType
from .machine_core import MachineValue
from .machine_host import MachineEventReceipt, MachineHost, MachineOpSpec, MachineStart
from .policies import StepPolicy
from .runtime import RunContext

if TYPE_CHECKING:
    from .machine import Machine


class MachineNode(Node):
    _expand_single_return = False

    def __init__(self, machine: Machine, op: Literal["start", "send"]) -> None:
        if op == "start":

            def operation(input: BaseModel | dict[str, JsonValue]) -> MachineStart:
                raise RuntimeError("MACHINE start requires a machine-capable host")
        else:

            def operation(
                key: tuple[JsonValue, ...] | list[JsonValue],
                event: str,
                payload: BaseModel | JsonValue = None,
                idempotency_key: str | None = None,
            ) -> MachineEventReceipt:
                raise RuntimeError("MACHINE send requires a machine-capable host")

        operation.__name__ = f"{machine.builder.__name__}_{op}"
        super().__init__(operation, NodeType.MACHINE, policy=StepPolicy(retries=0))
        self.spec = MachineOpSpec(machine=machine.builder.__name__, op=op)
        self.machine = machine


async def execute_machine_node(
    node: MachineNode,
    args: list[MachineValue | tuple[JsonValue, ...]],
    kwargs: dict[str, MachineValue | tuple[JsonValue, ...]],
    host: MachineHost,
    context: RunContext,
) -> MachineStart | MachineEventReceipt:
    """Resolve the same call signature used by graph binding, then call the host."""
    import inspect

    bound = inspect.signature(node.fn).bind(*args, **kwargs)
    bound.apply_defaults()
    if node.spec.op == "start":
        value = node.machine.input_type.model_validate(bound.arguments["input"]).model_dump(
            mode="json"
        )
        return await host.start(node.spec.machine, node.machine.key(value), value)
    key = TypeAdapter(tuple[JsonValue, ...]).validate_python(bound.arguments["key"])
    event = TypeAdapter(str).validate_python(bound.arguments["event"], strict=True)
    payload = bound.arguments["payload"]
    wire = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    idempotency_key = bound.arguments["idempotency_key"]
    return await node.machine.send(
        key,
        event,
        wire,
        idempotency_key=context.step_key if idempotency_key is None else idempotency_key,
        host=host,
    )
