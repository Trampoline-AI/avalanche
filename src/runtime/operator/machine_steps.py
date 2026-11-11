"""Typed host-operation requests and asynchronous scheduler replies."""

from __future__ import annotations

import inspect
from concurrent.futures import Future
from threading import Lock

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from avalanche.machine_host import (
    MachineEventReceipt,
    MachineOpSpec,
    MachinePayload,
    payload_to_json,
)
from avalanche.machine_nodes import MachineNode
from avalanche.references import prepare_run_references
from avalanche.run_steps import RunNode, RunOpSpec, RunStart, operation_enabled
from avalanche.runtime import RunContext

type HostStepOutput = RunStart | MachineEventReceipt | None


class MachineStepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str
    sender_run_id: str | None = None
    spec: RunOpSpec | MachineOpSpec
    enabled: bool = True
    target: str = ""
    input: dict[str, JsonValue] | None = None
    key: list[JsonValue] | str | None = None
    event: str = ""
    payload: JsonValue = None
    idempotency_key: str

    @classmethod
    def bind(
        cls,
        node: MachineNode | RunNode,
        args: list[MachinePayload],
        kwargs: dict[str, MachinePayload],
        context: RunContext,
    ) -> MachineStepRequest:
        bound = inspect.signature(node.fn).bind(*args, **kwargs)
        bound.apply_defaults()
        if not operation_enabled(bound.arguments["when"]):
            return cls(
                node_id=context.node_id,
                sender_run_id=context.run_id,
                spec=node.spec,
                enabled=False,
                idempotency_key=context.step_key,
            )
        if isinstance(node, RunNode):
            value = prepare_run_references(bound.arguments["input"])
            wire = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
            return cls(
                node_id=context.node_id,
                sender_run_id=context.run_id,
                spec=node.spec,
                input=wire,
                target=bound.arguments["target"],
                idempotency_key=context.step_key,
            )
        return cls(
            node_id=context.node_id,
            sender_run_id=context.run_id,
            spec=node.spec,
            key=TypeAdapter(list[JsonValue] | str).validate_python(bound.arguments["key"]),
            event=bound.arguments["event"],
            payload=payload_to_json(prepare_run_references(bound.arguments["payload"])),
            idempotency_key=bound.arguments["idempotency_key"] or context.step_key,
        )


class MachineStepResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str
    output: HostStepOutput = None
    error: str | None = None


class MachineStepPending:
    def __init__(self) -> None:
        self.lock = Lock()
        self.pending: dict[str, Future[HostStepOutput]] = {}

    def register(self, request: MachineStepRequest) -> Future[HostStepOutput]:
        future: Future[HostStepOutput] = Future()
        with self.lock:
            if request.node_id in self.pending:
                raise ValueError("Host step already pending")
            self.pending[request.node_id] = future
        return future

    def resolve(self, response: MachineStepResponse) -> None:
        with self.lock:
            future = self.pending.pop(response.node_id)
        if response.error is not None:
            future.set_exception(ValueError(response.error))
        else:
            future.set_result(response.output)
