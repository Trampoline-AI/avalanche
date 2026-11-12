"""Native-async deterministic machine driver for SDK operator tests.

The synchronous facade runs a private event loop only while driving work; it
never creates a thread. Async callers use test_async/asend/aadvance_time.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections import deque
from collections.abc import Coroutine, Mapping
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from .dag import (
    MapFuture,
    Node,
    NodeFuture,
    ParallelTasks,
    Workflow,
    _bind_implicit_parent_results,
    _build_node_binding_plan,
    _collect_implicit_parent_results,
    _inspect_providers,
    _inspect_runtime_params,
    _resolve_input_refs,
    _resolve_to_ref,
    _reverse_graph,
)
from .local_waits import LocalPrincipal, Reconcile, WaitReceipt, WaitRefusalError
from .machine_core import MachineRecord, reduce_machine
from .machine_host import (
    MachineEventReceipt,
    MachineKey,
    MachinePayload,
    payload_to_json,
)
from .machine_nodes import MachineNode, execute_machine_node
from .machine_waits import MachineWaits
from .policies import MAX_GENERATION, Event, NonRetryableError, OutcomeUnknownError, event
from .run_handle import RunHandle
from .run_keys import canonical_json
from .run_steps import RunNode, RunStart, RunTarget, operation_enabled
from .runtime import BaseInput, RunContext
from .runtime.providers import PROVIDERS
from .waits import WaitNode

if TYPE_CHECKING:
    from .machine import Machine

LocalValue = BaseModel | JsonValue


class _Pending(Enum):
    HOLD = "hold"


class MachineHistory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state: str
    event: str | None
    source: Literal["internal", "external", "timer"] | None


class MachineTest:
    def __init__(
        self,
        machine: Machine,
        input: BaseInput | Mapping[str, JsonValue],
        *,
        stub: Mapping[Node, list[LocalValue]] | None,
        host: InMemoryMachineHost | None = None,
    ) -> None:
        self.host = host if host is not None else InMemoryMachineHost(stub=stub)
        self.machine = machine
        self.spec = machine.spec
        self.input = machine.input_type.model_validate(input)
        self.state = self.spec.initial
        self.history: list[MachineHistory] = []
        self.handle: RunHandle[LocalValue] = RunHandle(uuid4().hex)
        self.waits = MachineWaits(self.handle.run_id)
        self.handle.waits = self.waits
        self._stubs = {node: deque(values) for node, values in (stub or {}).items()}
        self._busy = False
        self._values: dict[str, LocalValue] = {}
        self._payload: LocalValue = None
        self._work_done = False
        self._internal_consumed = False
        self._visit = 0
        self._enter()

    @staticmethod
    def _sync[T](coroutine: Coroutine[None, None, T]) -> T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        coroutine.close()
        raise RuntimeError("use machine.test_async/asend/aadvance_time inside an event loop")

    def _enter(self) -> None:
        self._visit += 1
        declaration = self.spec.states[self.state]
        self.waits.enter(self.machine.payloads[self.state], declaration.defer)
        self._due = [
            (self.waits.now() + timer.delay_seconds, timer.target)
            for timer in declaration.timers
        ]
        self._values = {}
        self._map_values: dict[str, dict[int, LocalValue]] = {}
        self._holds: set[str] = set()
        self._attempts: dict[str, int] = {}
        self._work_done = False
        self._internal_consumed = False
        work = self.machine.works[declaration.work]
        self.handle.node_aliases = {slug: name for name, slug in work.node_slugs.items()}
        self.waits.add_visit_waits(
            {
                name: (ref.node.spec, ref.node.model)
                for name, ref in work.nodes.items()
                if isinstance(ref.node, WaitNode)
            }
        )

    def send(
        self, name: str, payload: BaseModel | dict[str, JsonValue] | None = None
    ) -> WaitReceipt:
        receipt = self.handle.send_event(
            name,
            {} if payload is None else payload,
            idempotency_key=uuid4().hex,
        )
        if not self._busy:
            self._sync(self._drive())
        return receipt

    async def asend(
        self,
        name: str,
        payload: BaseModel | dict[str, JsonValue] | None = None,
    ) -> WaitReceipt:
        receipt = self.handle.send_event(
            name,
            {} if payload is None else payload,
            idempotency_key=uuid4().hex,
        )
        if not self._busy:
            await self._drive()
        return receipt

    def decide(
        self,
        node: str,
        payload: BaseModel | dict[str, JsonValue],
        *,
        reason: str,
    ) -> WaitReceipt:
        receipt = self.handle.decide(node, payload, reason=reason, idempotency_key=uuid4().hex)
        if not self._busy:
            self._sync(self._drive())
        return receipt

    async def adecide(
        self,
        node: str,
        payload: BaseModel | dict[str, JsonValue],
        *,
        reason: str,
    ) -> WaitReceipt:
        receipt = self.handle.decide(node, payload, reason=reason, idempotency_key=uuid4().hex)
        if not self._busy:
            await self._drive()
        return receipt

    def advance_time(self, duration: timedelta) -> None:
        self.handle.advance_time(duration)
        if not self._busy:
            self._sync(self._drive())

    async def aadvance_time(self, duration: timedelta) -> None:
        self.handle.advance_time(duration)
        if not self._busy:
            await self._drive()

    def _transition(
        self,
        target: str,
        name: str | None,
        source: Literal["internal", "external", "timer"],
        payload: LocalValue,
    ) -> None:
        self.history.append(MachineHistory(state=self.state, event=name, source=source))
        if payload is not None:
            self._payload = payload
        self.state = target
        self._enter()  # Cancels old timers and drops unaccepted buffered events.

    async def _drive(self) -> None:
        if self._busy or self.handle.done():
            return
        self._busy = True
        try:
            while True:
                if self.handle.cancel_requested():
                    self.handle._execution_receipts.cancel()
                    self.handle._future.cancel()
                    return
                declaration = self.spec.states[self.state]
                work = self.machine.works[declaration.work]
                if not self._work_done:
                    if not await self._run_work():
                        return
                    self._work_done = True
                    if self.handle.cancel_requested():
                        continue
                result = self._output(work.returns)
                result = work.validate_output(result)
                reduction = reduce_machine(
                    self.spec,
                    MachineRecord(self.state, self._payload, self._internal_consumed),
                    tuple(self.waits.buffer),
                    self._due,
                    result,
                    self.waits.now(),
                    work_done=self._work_done,
                )
                if reduction.finished:
                    self.history.append(
                        MachineHistory(state=self.state, event=None, source=None)
                    )
                    self.handle._set_execution_receipts(())
                    self.handle._future.set_result(result)
                    return
                self._internal_consumed = reduction.record.internal_consumed
                transition = reduction.transition
                if transition is None:
                    return
                payload = transition.payload
                if reduction.consumed_index is not None:
                    # The inbox adapter validates a formerly deferred payload at consumption.
                    consumed = self.waits.consume()
                    assert consumed is not None
                    payload = consumed.payload
                self._transition(
                    transition.target, transition.event, transition.source, payload
                )
        except Exception as exc:
            self.handle._execution_receipts.set_exception(exc)
            self.handle._future.set_exception(exc)
            raise
        finally:
            self._busy = False

    def _output(self, value):
        if isinstance(value, NodeFuture):
            target = value.chain_end
            if isinstance(target, ParallelTasks):
                return tuple(self._output(branch) for branch in target.branches)
            return _resolve_to_ref(target, self._values)
        if isinstance(value, tuple):
            return tuple(self._output(item) for item in value)
        return value

    @property
    def work(self) -> Workflow:
        return self.machine.works[self.spec.states[self.state].work]

    async def _run_work(self) -> bool:
        work = self.work
        dependencies = _reverse_graph(work.graph)
        while len(self._values) < len(work.nodes):
            ready = [
                name
                for name in work._topological_sort()
                if name not in self._values
                and all(parent in self._values for parent in dependencies.get(name, ()))
            ]
            executable: list[str] = []
            for name in ready:
                ref = work.nodes[name]
                if name in self._holds and not self.waits.resolved(name):
                    continue
                if isinstance(ref.node, WaitNode):
                    self.waits.arm(name)
                    if self.waits.resolved(name):
                        self._values[name] = self.waits.result(name)
                else:
                    executable.append(name)
            if executable:
                # TaskGroup owns sibling cancellation on a failed visit; no task leaks.
                async with asyncio.TaskGroup() as group:
                    tasks = {
                        name: group.create_task(self._invoke(work.nodes[name], dependencies))
                        for name in executable
                    }
                for name, task in tasks.items():
                    value = task.result()
                    if value is not _Pending.HOLD:
                        self._values[name] = value
                if not any(name in self._values for name in ready):
                    return False
            elif not any(name in self._values for name in ready):
                return False
        return True

    def _stub(self, node: Node) -> LocalValue:
        if not self._stubs[node]:
            raise NonRetryableError(f"stub exhausted for step {node.name}")
        value = self._stubs[node].popleft()
        if isinstance(value, Event) and value.name is not None and value.name not in node.emits:
            raise NonRetryableError(f"undeclared event {value.name!r}")
        annotation = inspect.signature(node.fn).return_annotation
        if annotation is inspect.Signature.empty:
            return value
        wire = value.model_dump(mode="python") if isinstance(value, BaseModel) else value
        validated = TypeAdapter(annotation).validate_python(wire)
        return (
            event(value.name, validated.output)
            if isinstance(value, Event) and value.name is not None
            else validated
        )

    async def _invoke(
        self, ref: NodeFuture, dependencies: dict[str, list[str]]
    ) -> LocalValue | _Pending:
        node = ref.node
        work = self.work
        if isinstance(ref, MapFuture):
            mapped, broadcast = _resolve_input_refs(
                [_resolve_to_ref(value, self._values) for value in ref.args],
                ref.kwargs,
                self.input,
                node.name,
                work.name,
            )
            if not all(isinstance(value, list) for value in mapped):
                raise NonRetryableError("map positional inputs must resolve to lists")
            lengths = {len(value) for value in mapped}
            if len(lengths) != 1:
                raise NonRetryableError("map positional inputs must have equal lengths")
            count = lengths.pop()
            if count > ref.map_spec.max_items:
                raise NonRetryableError("map_overflow")
            results = self._map_values.setdefault(ref.future_id, {})
            for index, values in enumerate(zip(*mapped)):
                if index in results:
                    continue
                child = NodeFuture(
                    node=node,
                    future_id=f"{ref.future_id}[{index}]",
                    node_slug=f"{ref.node_slug}[{index}]",
                    graph_ref=ref.graph_ref,
                    args=values,
                    kwargs=broadcast,
                )
                value = await self._invoke(child, dependencies)
                if value is not _Pending.HOLD:
                    results[index] = value
            return (
                [results[index] for index in range(count)]
                if len(results) == count
                else _Pending.HOLD
            )
        if ref.future_id in self._holds:
            if not self.waits.resolved(ref.future_id):
                return _Pending.HOLD
            answer = self.waits.result(ref.future_id)
            assert isinstance(answer, Reconcile)
            self._holds.remove(ref.future_id)
            if answer.kind == "done":
                annotation = inspect.signature(node.fn).return_annotation
                return TypeAdapter(annotation).validate_python(answer.output)
            if answer.kind == "fail" or self._attempts[ref.future_id] >= MAX_GENERATION:
                raise OutcomeUnknownError("reconciliation failed or generation ceiling")
        if isinstance(node, RunNode):
            if node.definition is not None:
                self.host.register(node.definition)
            args = [_resolve_to_ref(value, self._values) for value in ref.args]
            kwargs = {
                name: _resolve_to_ref(value, self._values) for name, value in ref.kwargs.items()
            }
            args, kwargs = _resolve_input_refs(args, kwargs, self.input, node.name, work.name)
            bound = inspect.signature(node.fn).bind(*args, **kwargs)
            bound.apply_defaults()
            if not operation_enabled(bound.arguments["when"]):
                return None
            target = TypeAdapter(str).validate_python(bound.arguments["target"], strict=True)
            return await self.host._start_run(target, bound.arguments["input"])
        if isinstance(node, MachineNode):
            self.host.register(node.machine)
            context = RunContext(
                run_id=self.handle.run_id,
                workflow_name=work.name,
                node_id=f"v{self._visit}.{ref.future_id}" if self._visit else ref.future_id,
                node_name=node.name,
                node_slug=ref.node_slug,
            )
            args = [_resolve_to_ref(value, self._values) for value in ref.args]
            kwargs = {
                name: _resolve_to_ref(value, self._values) for name, value in ref.kwargs.items()
            }
            args, kwargs = _resolve_input_refs(args, kwargs, self.input, node.name, work.name)
            return await execute_machine_node(node, args, kwargs, self.host, context)
        if node in self._stubs:
            return self._stub(node)
        plan = _build_node_binding_plan(
            node.fn, ref.args, ref.kwargs, ref._incoming_refs, providers=PROVIDERS
        )
        context = RunContext(
            run_id=self.handle.run_id,
            workflow_name=work.name,
            node_id=f"v{self._visit}.{ref.future_id}" if self._visit else ref.future_id,
            node_name=node.name,
            node_slug=ref.node_slug,
        )
        runtime = _inspect_runtime_params(
            node.fn, (), plan.kwargs, self.input, context, context
        )
        inspected = _inspect_providers(node.fn, plan.kwargs, PROVIDERS)
        providers = {
            name: next(provider for provider in PROVIDERS if provider.can_resolve(value))
            for name, value in inspected.items()
        }
        consuming = {name for name, provider in providers.items() if provider.consumes_upstream}
        skipped = set(runtime) | (set(providers) - consuming)
        args = [_resolve_to_ref(value, self._values) for value in plan.args]
        kwargs = {
            name: _resolve_to_ref(value, self._values)
            for name, value in plan.kwargs.items()
            if name not in runtime and name not in providers
        }
        args, kwargs = _resolve_input_refs(args, kwargs, self.input, node.name, work.name)
        if not ref.args and not plan.has_explicit_provider_selectors:
            upstream = _collect_implicit_parent_results(
                ref, work.nodes, self._values, dependencies, ref.future_id
            )
            if not upstream and self._payload is not None:
                unbound = [
                    p
                    for p in inspect.signature(node.fn).parameters.values()
                    if p.name not in skipped
                    and p.name not in consuming
                    and p.name not in kwargs
                ]
                if unbound:
                    upstream = [self._payload]
            args, implicit = _bind_implicit_parent_results(
                node.fn, upstream, skipped, consuming, kwargs
            )
            kwargs.update(implicit)
        actual_fn = node.fn
        if providers:
            from .dag import _data_param_position
            from .types import ParamContext

            for name, provider in providers.items():
                selector = plan.provider_selectors.get(name)
                parents = dependencies.get(ref.future_id, [])
                parent_results = [self._values[parent] for parent in parents]
                slugs = [work.node_slugs[parent] for parent in parents]
                position = _data_param_position(node.fn, name, skipped, consuming)
                if selector is not None:
                    parent_results = [_resolve_to_ref(selector, self._values)]
                    slugs, position = [work.node_slugs[selector.future_id]], 0
                resolved = provider.resolve(
                    inspected[name],
                    ParamContext(
                        parent_results=parent_results,
                        param_position=position,
                        node_name=node.name,
                        node_slug=ref.node_slug,
                        upstream_node_slugs=slugs,
                        run_id=self.handle.run_id,
                        executor_type="local",
                    ),
                )
                wrapper = provider.create_wrapper(inspected[name], actual_fn, {name: resolved})
                if wrapper is not None:
                    actual_fn = wrapper
                else:
                    kwargs[name] = resolved
        if plan.positional_call_adapter is not None:
            from .dag import _adapt_positional_call

            actual_fn = _adapt_positional_call(actual_fn, node.fn, plan.positional_call_adapter)
        kwargs.update(runtime)
        args = [
            value.output if isinstance(value, Event) and value.name is not None else value
            for value in args
        ]
        kwargs = {
            name: value.output if isinstance(value, Event) and value.name is not None else value
            for name, value in kwargs.items()
        }
        from .runtime.context import _current_run_context

        token = _current_run_context.set(context)
        try:
            used = self._attempts.get(ref.future_id, 0)
            rounds = max(1, node.policy.effective_retries + 1 - used)
            for attempt in range(rounds):
                self._attempts[ref.future_id] = self._attempts.get(ref.future_id, 0) + 1
                try:
                    value = actual_fn(*args, **kwargs)
                    return await value if inspect.isawaitable(value) else value
                except NonRetryableError:
                    raise
                except OutcomeUnknownError:
                    if node.policy.on_uncertain == "hold":
                        annotation = inspect.signature(node.fn).return_annotation
                        if annotation is inspect.Signature.empty:
                            raise TypeError("hold steps require a return annotation") from None
                        self.waits.add_hold(ref.future_id, TypeAdapter(annotation))
                        self.waits.arm(ref.future_id)
                        self._holds.add(ref.future_id)
                        return _Pending.HOLD
                    if node.policy.on_uncertain == "fail" or attempt == rounds - 1:
                        raise
                except Exception:
                    if attempt == rounds - 1:
                        raise
        finally:
            _current_run_context.reset(token)
        raise AssertionError("unreachable retry ladder")


class InMemoryMachineHost:
    """Test-only host: one instance per canonical key and real machine semantics."""

    def __init__(self, *, stub: Mapping[Node, list[LocalValue]] | None = None) -> None:
        self.machines: dict[str, Machine] = {}
        self.flows: dict[str, Workflow] = {}
        self.flow_runs: list[WorkflowTest] = []
        self.keyed_flow_runs: dict[tuple[str, str], WorkflowTest] = {}
        self.instances: dict[tuple[str, str], MachineTest] = {}
        self.receipts: dict[tuple[str, str, str, str], tuple[str, MachineEventReceipt]] = {}
        self.stub = stub

    def register(self, target: RunTarget) -> None:
        from .machine import Machine

        if isinstance(target, Machine):
            name = target.builder.__name__
            if name in self.machines and self.machines[name] is not target:
                raise ValueError(f"duplicate machine name {name!r}")
            self.machines[name] = target
        else:
            flow = target if isinstance(target, Workflow) else target()
            self.flows[flow.name] = flow

    @staticmethod
    def _key(key: MachineKey) -> str:
        return canonical_json(key)

    async def _start_run(
        self, target: str, input: BaseInput | dict[str, JsonValue] | None
    ) -> RunStart:
        """Test scheduler path, not a public host-start transport API."""
        if target in self.flows:
            flow = self.flows[target]
            key = flow.key(input) if flow.key_fields else None
            address = (target, self._key(key)) if key is not None else None
            if address is not None and address in self.keyed_flow_runs:
                return RunStart(
                    run_id=self.keyed_flow_runs[address].handle.run_id,
                    key=list(key),
                    created=False,
                )
            driver = WorkflowTest(flow, input, stub=self.stub, host=self)
            self.flow_runs.append(driver)
            if address is not None:
                self.keyed_flow_runs[address] = driver
            await driver._drive()
            return RunStart(
                run_id=driver.handle.run_id,
                key=list(key) if key is not None else None,
                created=True,
            )
        definition = self.machines[target]
        key = definition.key(input)
        address = (target, self._key(key))
        if address in self.instances:
            return RunStart(
                run_id=self.instances[address].handle.run_id, key=list(key), created=False
            )
        driver = MachineTest(definition, input, stub=self.stub, host=self)
        self.instances[address] = driver
        await driver._drive()
        return RunStart(run_id=driver.handle.run_id, key=list(key), created=True)

    async def send(
        self,
        machine: str,
        key: MachineKey | str,
        event: str,
        payload: MachinePayload,
        idempotency_key: str,
    ) -> MachineEventReceipt:
        if isinstance(key, str):
            key = next(
                definition.key(driver.input)
                for (name, _), driver in self.instances.items()
                if name == machine and driver.handle.run_id == key
                for definition in (self.machines[machine],)
            )
        encoded = self._key(key)
        address = (machine, encoded, event, idempotency_key)
        payload = payload_to_json(payload)
        body = json.dumps(
            {} if payload is None else payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        if address in self.receipts:
            previous, receipt = self.receipts[address]
            if previous != body:
                raise WaitRefusalError("conflict", 409)
            return receipt.model_copy(update={"duplicate": True})
        driver = self.instances[(machine, encoded)]
        if driver.handle.done():
            raise WaitRefusalError("run_closed", 409)
        if payload is not None and not isinstance(payload, dict):
            raise WaitRefusalError("event_payload_must_be_object", 422)
        accepted = driver.waits.send_event(
            event,
            {} if payload is None else payload,
            idempotency_key=idempotency_key,
            principal=LocalPrincipal(),
        )
        receipt = MachineEventReceipt(
            receipt_id=accepted.receipt_id,
            key=event,
            accepted_at=accepted.accepted_at.timestamp(),
            duplicate=False,
            status="accepted" if accepted.reason == "accepted" else "ignored",
        )
        self.receipts[address] = (body, receipt)
        if not driver._busy:
            await driver._drive()
        return receipt


class WorkflowTest(MachineTest):
    """Reuse the test visit DAG driver, without a synthetic machine definition."""

    def __init__(
        self,
        workflow: Workflow,
        input: BaseInput | Mapping[str, JsonValue] | None,
        *,
        stub: Mapping[Node, list[LocalValue]] | None = None,
        host: InMemoryMachineHost | None = None,
        targets: tuple[RunTarget, ...] = (),
    ) -> None:
        self._workflow = workflow
        self.host = host if host is not None else InMemoryMachineHost(stub=stub)
        for target in targets:
            self.host.register(target)
        self.input = workflow.input_type.model_validate(input) if workflow.input_type else None
        self.handle: RunHandle[LocalValue] = RunHandle(uuid4().hex)
        self.waits = MachineWaits(self.handle.run_id)
        self.handle.waits = self.waits
        self.handle.node_aliases = {slug: name for name, slug in workflow.node_slugs.items()}
        self.waits.add_visit_waits(
            {
                name: (ref.node.spec, ref.node.model)
                for name, ref in workflow.nodes.items()
                if isinstance(ref.node, WaitNode)
            }
        )
        self._stubs = {node: deque(values) for node, values in (stub or {}).items()}
        self._values: dict[str, LocalValue] = {}
        self._map_values: dict[str, dict[int, LocalValue]] = {}
        self._holds: set[str] = set()
        self._attempts: dict[str, int] = {}
        self._payload = None
        self._visit = 0
        self._busy = False

    @property
    def work(self) -> Workflow:
        return self._workflow

    async def _drive(self) -> None:
        if self._busy or self.handle.done():
            return
        self._busy = True
        try:
            if await self._run_work():
                result = self.work.validate_output(self._output(self.work.returns))
                self.handle._set_execution_receipts(())
                self.handle._future.set_result(result)
        except Exception as exc:
            self.handle._execution_receipts.set_exception(exc)
            self.handle._future.set_exception(exc)
            raise
        finally:
            self._busy = False
