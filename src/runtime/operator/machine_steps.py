"""Typed coordinator-to-host machine operation, with asynchronous scheduler replies."""

from __future__ import annotations

import inspect
from concurrent.futures import Future
from threading import Lock

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from avalanche.machine_host import MachineEventReceipt, MachineOpSpec, MachineStart
from avalanche.machine_nodes import MachineNode
from avalanche.runtime import RunContext


class MachineStepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str
    spec: MachineOpSpec
    input: dict[str, JsonValue] | None = None
    key: list[JsonValue] | None = None
    event: str = ""
    payload: JsonValue = None
    idempotency_key: str

    @classmethod
    def bind(
        cls, node: MachineNode, args: list, kwargs: dict, context: RunContext
    ) -> MachineStepRequest:
        bound = inspect.signature(node.fn).bind(*args, **kwargs)
        bound.apply_defaults()
        if node.spec.op == "start":
            value = bound.arguments["input"]
            wire = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
            return cls(
                node_id=context.node_id,
                spec=node.spec,
                input=wire,
                idempotency_key=context.step_key,
            )
        payload = bound.arguments["payload"]
        return cls(
            node_id=context.node_id,
            spec=node.spec,
            key=TypeAdapter(list[JsonValue]).validate_python(bound.arguments["key"]),
            event=bound.arguments["event"],
            payload=payload.model_dump(mode="json")
            if isinstance(payload, BaseModel)
            else payload,
            idempotency_key=bound.arguments["idempotency_key"] or context.step_key,
        )


class MachineStepResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str
    output: MachineStart | MachineEventReceipt | None = None
    error: str | None = None


class MachineStepPending:
    def __init__(self) -> None:
        self.lock = Lock()
        self.pending: dict[str, Future[MachineStart | MachineEventReceipt]] = {}

    def register(
        self, request: MachineStepRequest
    ) -> Future[MachineStart | MachineEventReceipt]:
        future: Future[MachineStart | MachineEventReceipt] = Future()
        with self.lock:
            if request.node_id in self.pending:
                raise ValueError("Machine step already pending")
            self.pending[request.node_id] = future
        return future

    def resolve(self, response: MachineStepResponse) -> None:
        with self.lock:
            future = self.pending.pop(response.node_id)
        if response.error is not None:
            future.set_exception(ValueError(response.error))
        elif response.output is None:
            future.set_exception(ValueError("Missing machine step result"))
        else:
            future.set_result(response.output)
