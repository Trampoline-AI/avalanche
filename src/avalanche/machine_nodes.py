"""Declared machine sends; only a machine-capable scheduler may execute them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import JsonValue, TypeAdapter

from .dag import Node, NodeType
from .machine_host import MachineEventReceipt, MachineHost, MachineOpSpec, MachinePayload
from .policies import StepPolicy
from .run_steps import operation_enabled
from .runtime import RunContext

if TYPE_CHECKING:
    from .machine import Machine


class MachineNode(Node):
    _expand_single_return = False

    def __init__(self, machine: Machine) -> None:
        def operation(
            key: tuple[JsonValue, ...] | list[JsonValue] | str,
            event: str,
            payload: MachinePayload = None,
            idempotency_key: str | None = None,
            *,
            when: bool = True,
        ) -> MachineEventReceipt | None:
            raise RuntimeError("MACHINE send requires a machine-capable scheduler host")

        operation.__name__ = f"{machine.builder.__name__}_send"
        super().__init__(operation, NodeType.MACHINE, policy=StepPolicy(retries=0))
        self.spec = MachineOpSpec(machine=machine.builder.__name__)
        self.machine = machine


async def execute_machine_node(
    node: MachineNode,
    args: list[MachinePayload | tuple[JsonValue, ...]],
    kwargs: dict[str, MachinePayload | tuple[JsonValue, ...]],
    host: MachineHost,
    context: RunContext,
) -> MachineEventReceipt | None:
    """Resolve graph skip before validating an active send's arguments."""
    import inspect

    bound = inspect.signature(node.fn).bind(*args, **kwargs)
    bound.apply_defaults()
    if not operation_enabled(bound.arguments["when"]):
        return None
    key = TypeAdapter(tuple[JsonValue, ...] | str).validate_python(bound.arguments["key"])
    event = TypeAdapter(str).validate_python(bound.arguments["event"], strict=True)
    idempotency_key = bound.arguments["idempotency_key"]
    return await node.machine.send(
        key,
        event,
        bound.arguments["payload"],
        idempotency_key=context.step_key if idempotency_key is None else idempotency_key,
        host=host,
    )
