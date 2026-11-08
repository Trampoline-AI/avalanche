"""Declarative machine authoring; builders run once, work runs on state entry."""

from __future__ import annotations

import inspect
import warnings
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from functools import update_wrapper
from types import FrameType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .machine_remote import MachineEventReceipt

from pydantic import BaseModel, JsonValue

from .dag import MapFuture, Node, NodeFuture, Workflow, WorkflowContext, _workflow_context
from .machine_spec import (
    MachineMapSpec,
    MachineSpec,
    MachineStateSpec,
    MachineTimerSpec,
    MachineTransitionSpec,
    MachineWorkSpec,
)
from .runtime import BaseInput


@dataclass
class _Capture:
    states: list[State] = field(default_factory=list)
    frame: FrameType | None = None


_capture: ContextVar[_Capture | None] = ContextVar("avalanche_machine", default=None)


@dataclass
class _Arrow:
    state: State
    event: str | None = None
    payload: type[BaseModel] | None = None
    delay: timedelta | None = None

    def __rshift__(self, target: State) -> State:
        if not isinstance(target, State):
            raise ValueError("unknown target state: use a state from this machine")
        self.state.edges.append((self, target))
        return target


@dataclass(eq=False)
class State:
    work: Node | Callable[[], Workflow]
    initial: bool
    final: bool
    defer: tuple[str, ...]
    edges: list[tuple[_Arrow, State]] = field(default_factory=list)

    def on(self, name: str, *, payload: type[BaseModel] | None = None) -> _Arrow:
        if not name:
            raise ValueError("event names must be nonempty")
        if payload is not None and not issubclass(payload, BaseModel):
            raise TypeError("event payload must be a Pydantic model")
        return _Arrow(self, event=name, payload=payload)

    def after(self, delay: timedelta) -> _Arrow:
        if delay.total_seconds() < 0:
            raise ValueError("timer delay must be nonnegative")
        return _Arrow(self, delay=delay)


def state(
    work: Node | Callable[[], Workflow],
    *,
    initial: bool = False,
    final: bool = False,
    defer: list[str] | tuple[str, ...] = (),
) -> State:
    capture = _capture.get()
    if capture is None:
        raise RuntimeError("ava.state must be used inside an @ava.machine builder")
    frame = inspect.currentframe()
    assert frame is not None
    capture.frame = frame.f_back
    del frame
    result = State(work, initial, final, tuple(defer))
    capture.states.append(result)
    return result


def _work_graph(work: Node | Callable[[], Workflow], input_type: type[BaseInput]) -> Workflow:
    if not isinstance(work, Node):
        graph = work()
        if not isinstance(graph, Workflow):
            raise TypeError("state work must be an ava.step or ava.workflow")
        return graph
    context = WorkflowContext()
    token = _workflow_context.set(context)
    try:
        result = work()
    finally:
        _workflow_context.reset(token)
    return Workflow(
        graph=context.graph,
        nodes=context.node_instances,
        name=work.name,
        returns=result,
        input_type=input_type,
        emits=work.emits,
    )


def work_spec(work: Workflow) -> MachineWorkSpec:
    if len(work.nodes) > 64:
        raise ValueError(f"state visit work {work.name!r} exceeds 64 nodes")
    output_node = work.output_node
    schema = (
        work.output_type.model_json_schema()
        if work.output_type is not None
        else output_node.node.step_interface.step_output.json_schema
        if output_node is not None
        else None
    )
    return MachineWorkSpec(
        node_ids=tuple(work._topological_sort()),
        graph={name: tuple(children) for name, children in work.graph.items()},
        node_slugs=work.node_slugs,
        node_types={name: ref.node.node_type.value for name, ref in work.nodes.items()},
        node_policies={name: ref.node.policy for name, ref in work.nodes.items()},
        node_waits=work.node_waits,
        node_output_schemas={
            name: ref.node.step_interface.step_output.json_schema
            for name, ref in work.nodes.items()
            if ref.node.step_interface.step_output.json_schema is not None
        },
        node_emits={name: ref.node.emits for name, ref in work.nodes.items()},
        map_specs={
            name: MachineMapSpec(
                **ref.map_spec.model_dump(),
                sources=tuple(
                    arg.future_id if isinstance(arg, NodeFuture) else len(arg)
                    for arg in ref.args
                ),
            )
            for name, ref in work.nodes.items()
            if isinstance(ref, MapFuture)
        },
        emits=work.emits,
        output_node_id=output_node.future_id if output_node is not None else None,
        output_schema=schema,
    )


class MachineDiscoveryWarning(UserWarning):
    """A declared event has no accepting state."""


class _MachineWorkflow(Workflow):
    def run(self, *args, **kwargs):
        raise RuntimeError(
            "machine execution requires a machine-capable host; use machine.test locally"
        )


class Machine:
    """Cached discovery builder and local test entry point, not a live instance."""

    __avalanche_workflow__ = True
    __avalanche_machine__ = True

    def __init__(
        self,
        builder: Callable[[], State | None],
        input_type: type[BaseInput],
        key: tuple[str, ...],
        key_format: str | None,
    ) -> None:
        update_wrapper(self, builder)
        self.builder = builder
        self.input_type = input_type
        self.key_fields = key
        self.key_format = key_format
        self._definition: Workflow | None = None
        self.works: dict[str, Workflow] = {}
        self.payloads: dict[str, dict[str, type[BaseModel] | None]] = {}

    def __call__(self) -> Workflow:
        if self._definition is not None:
            return self._definition
        capture = _Capture()
        token = _capture.set(capture)
        try:
            self.builder()
            if capture.frame is None:
                raise ValueError("machine requires exactly one initial state")
            names = {
                value: name
                for name, value in capture.frame.f_locals.items()
                if isinstance(value, State)
            }
        finally:
            capture.frame = None
            _capture.reset(token)
        if set(names) != set(capture.states):
            raise ValueError("each state must be assigned a unique builder-local name")
        states: dict[str, MachineStateSpec] = {}
        works: dict[str, Workflow] = {}
        captured_work: dict[Node | Callable[[], Workflow], Workflow] = {}
        payloads: dict[str, dict[str, type[BaseModel] | None]] = {}
        for definition, name in names.items():
            if definition.work not in captured_work:
                work = _work_graph(definition.work, self.input_type)
                if work.name in works:
                    raise ValueError(f"duplicate machine work name {work.name!r}")
                captured_work[definition.work] = work
                works[work.name] = work
            work = captured_work[definition.work]
            transitions: list[MachineTransitionSpec] = []
            timers: list[MachineTimerSpec] = []
            payloads[name] = {}
            for edge, target in definition.edges:
                if target not in names:
                    raise ValueError(f"unknown target state from {name}")
                if edge.event is not None:
                    transitions.append(
                        MachineTransitionSpec(
                            event=edge.event,
                            target=names[target],
                            payload_schema=(
                                edge.payload.model_json_schema()
                                if edge.payload is not None
                                else None
                            ),
                        )
                    )
                    payloads[name][edge.event] = edge.payload
                else:
                    assert edge.delay is not None
                    timers.append(
                        MachineTimerSpec(
                            delay_seconds=edge.delay.total_seconds(), target=names[target]
                        )
                    )
            states[name] = MachineStateSpec(
                work=work.name,
                initial=definition.initial,
                final=definition.final,
                defer=definition.defer,
                transitions=tuple(transitions),
                timers=tuple(timers),
            )
        initials = [name for name, declaration in states.items() if declaration.initial]
        if len(initials) != 1:
            raise ValueError("machine requires exactly one initial state")
        specs = {name: work_spec(work) for name, work in works.items()}
        all_emits = {event for spec in specs.values() for event in spec.emits}
        for name, declaration in states.items():
            for transition in declaration.transitions:
                if (
                    transition.event in all_emits
                    and transition.event not in specs[declaration.work].emits
                ):
                    raise ValueError(
                        f"internal event {transition.event!r} not declared in {name} work emits"
                    )
        accepted = {
            edge.event for declaration in states.values() for edge in declaration.transitions
        }
        deferred = {event for declaration in states.values() for event in declaration.defer}
        for event in sorted((all_emits | deferred) - accepted):
            warnings.warn(
                f"event {event!r} is accepted by no state",
                MachineDiscoveryWarning,
                stacklevel=2,
            )
        spec = MachineSpec(
            name=self.__name__,
            input_schema=self.input_type.model_json_schema(),
            key_fields=self.key_fields,
            key_format=self.key_format,
            initial=initials[0],
            states=states,
            work_graphs=specs,
        )
        result = _MachineWorkflow(
            defaultdict(list), {}, name=self.__name__, input_type=self.input_type
        )
        result.machine_spec = spec
        self.works, self.payloads, self._definition = works, payloads, result
        return result

    @property
    def spec(self) -> MachineSpec:
        spec = self().machine_spec
        assert spec is not None
        return spec

    def test(
        self,
        input: BaseInput | Mapping[str, JsonValue],
        *,
        stub: Mapping[Node, list[BaseModel | JsonValue]] | None = None,
    ):
        from .machine_test import MachineTest

        driver = MachineTest(self, input, stub=stub)
        driver._sync(driver._drive())
        return driver

    async def test_async(
        self,
        input: BaseInput | Mapping[str, JsonValue],
        *,
        stub: Mapping[Node, list[BaseModel | JsonValue]] | None = None,
    ):
        from .machine_test import MachineTest

        driver = MachineTest(self, input, stub=stub)
        await driver._drive()
        return driver

    async def start(
        self, input: BaseInput | Mapping[str, JsonValue], *, deployment_id: str | None = None,
        api_url: str | None = None, token: str | None = None,
        org: str | None = None, project: str | None = None,
    ) -> str:
        """Start once per key, returning the existing or newly created remote run ID."""
        from .machine_remote import start

        value = self.input_type.model_validate(input).model_dump(mode="json")
        return await start(
            self.builder.__name__, self.key(value), value, deployment_id=deployment_id,
            api_url=api_url, token=token, org=org, project=project)

    async def send(
        self, key: tuple[JsonValue, ...], event: str, *, idempotency_key: str,
        payload: BaseModel | JsonValue = None, api_url: str | None = None,
        token: str | None = None, org: str | None = None, project: str | None = None,
    ) -> MachineEventReceipt:
        """Send by instance key; retries must retain the same idempotency key."""
        from .machine_remote import send

        if len(key) != len(self.key_fields):
            raise ValueError("machine key has the wrong number of fields")
        return await send(self.builder.__name__, key, event, payload=payload,
                          idempotency_key=idempotency_key, api_url=api_url,
                          token=token, org=org, project=project)

    def key(self, input: BaseInput | Mapping[str, JsonValue]) -> tuple[JsonValue, ...]:
        fields = self.spec.key_fields
        value = self.input_type.model_validate(input).model_dump(mode="json")
        return tuple(value[field] for field in fields)


def machine(
    *,
    input: type[BaseInput],
    key: tuple[str, ...],
    key_format: str | None = None,
) -> Callable[[Callable[[], State | None]], Machine]:
    if not issubclass(input, BaseInput):
        raise TypeError("machine input must be a BaseInput model")
    return lambda builder: Machine(builder, input, key, key_format)
