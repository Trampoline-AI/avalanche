"""Native-async deterministic machine driver for SDK operator tests.

The synchronous facade runs a private event loop only while driving work; it
never creates a thread. Async callers use test_async/asend/aadvance_time.
"""

from __future__ import annotations

import asyncio
import inspect
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
    _bind_implicit_parent_results,
    _build_node_binding_plan,
    _collect_implicit_parent_results,
    _inspect_providers,
    _inspect_runtime_params,
    _resolve_input_refs,
    _resolve_to_ref,
    _reverse_graph,
)
from .local_waits import Reconcile, WaitReceipt
from .machine_waits import MachineWaits
from .policies import MAX_GENERATION, Event, NonRetryableError, OutcomeUnknownError, event
from .run_handle import RunHandle
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
    ) -> None:
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
                if declaration.final:
                    self.history.append(
                        MachineHistory(state=self.state, event=None, source=None)
                    )
                    self.handle._set_execution_receipts(())
                    self.handle._future.set_result(result)
                    return
                expired = [
                    (due, target) for due, target in self._due if due <= self.waits.now()
                ]
                if expired:
                    _, target = min(expired, key=lambda timer: timer[0])
                    self._transition(target, None, "timer", None)
                    continue
                if not self._internal_consumed:
                    self._internal_consumed = True
                    if isinstance(result, Event) and result.name is not None:
                        if result.name not in work.emits:
                            raise NonRetryableError(
                                f"undeclared workflow event {result.name!r}"
                            )
                        target = next(
                            (
                                t.target
                                for t in declaration.transitions
                                if t.event == result.name
                            ),
                            None,
                        )
                        if target is not None:
                            self._transition(target, result.name, "internal", result.output)
                            continue
                event = self.waits.consume()
                if event is None:
                    return
                target = next(
                    t.target for t in declaration.transitions if t.event == event.name
                )
                self._transition(target, event.name, "external", event.payload)
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

    async def _run_work(self) -> bool:
        work = self.machine.works[self.spec.states[self.state].work]
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
        work = self.machine.works[self.spec.states[self.state].work]
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
        if node in self._stubs:
            return self._stub(node)
        plan = _build_node_binding_plan(
            node.fn, ref.args, ref.kwargs, ref._incoming_refs, providers=PROVIDERS
        )
        context = RunContext(
            run_id=self.handle.run_id,
            workflow_name=work.name,
            node_id=ref.future_id,
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
