"""Contract tests for Ray ObjectRef materialization in Workflow.run().

These tests use a fake Ray-shaped executor built around the intended primitive:
``submit_with_status``. A user task returns its payload(s) plus a small **status
marker** produced by the *same* task (Ray ``num_returns + 1``). Fetching the
status ref surfaces a task exception without materializing the payload; fetching
a payload ref is real data movement.

The contract this locks in:
- the driver fetches **payloads** only when genuinely required (explicit
  workflow return, ``unwrap_result``);
- progress hooks and no-return draining fetch **status** refs only;
- a task failure still surfaces through the status ref;
- downstream user tasks receive parent **payload refs** (resolved worker-side);
- Stream passthrough detection does not driver-fetch the parent payload;
- indexed parent binding does not driver-fetch the parent payload.

The fake counts payload materializations and attributes each to the resolving
context, so a status/projection/driver path that touches a payload trips the
test.

The executor class is named ``RayExecutor`` because ``Workflow.run`` selects the
Ray path via ``type(executor).__name__ == "RayExecutor"``.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from functools import wraps
from typing import Any

import polars as pl
import pytest

import avalanche as ava
import avalanche.runtime.providers.stream as stream_mod
from avalanche._testing.rerun_helpers import (
    RerunSelectorInput,
    positional_only_selector_consume,
)
from avalanche.types import AppendResult, AppendResultHandle, LineagedResult
from runtime.operator.hooks import RunHooks

_STATUS = object()  # sentinel marking a status-marker return slot


@dataclass(eq=False)
class FakeObjectRef:
    """A Ray-shaped object reference. Hashable by identity (like ray.ObjectRef)."""

    task: "FakeTask | None" = None
    index: int | None = None  # which return slot of the task
    is_status: bool = False
    is_receipt: bool = False
    stored_label: str | None = None
    stored_value: Any = None
    is_stored: bool = False


@dataclass(eq=False)
class FakeTask:
    fn: Any
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    num_returns: int
    computed: bool = False
    outputs: tuple[Any, ...] = ()
    error: BaseException | None = None


class FakeRay:
    ObjectRef = FakeObjectRef

    def __init__(self, executor: "RayExecutor"):
        self.executor = executor
        self.wait_calls: list[tuple[int, int, float | None]] = []

    def wait(self, refs, *, num_returns=1, timeout=None):
        refs = list(refs)
        self.wait_calls.append((len(refs), num_returns, timeout))
        return refs[:num_returns], refs[num_returns:]


class RayExecutor:
    """Fake distributed executor modeling ``submit_with_status``.

    Named ``RayExecutor`` so ``Workflow.run`` treats it as the Ray path.
    """

    def __init__(self):
        self.ray = FakeRay(self)
        self.submissions: list[tuple[str, dict[str, str]]] = []
        self.driver_payload_gets: list[str] = []
        self.worker_payload_gets: list[str] = []
        self.status_gets: list[str] = []
        self.receipt_gets: list[str] = []
        self.payload_resolutions: list[tuple[str, str]] = []  # (context, task)
        self._context: str = "__driver__"

    # -- submission -------------------------------------------------------

    def submit(self, fn, *args, num_returns=1, **kwargs):
        self.submissions.append((fn.__name__, {k: type(v).__name__ for k, v in kwargs.items()}))
        task = FakeTask(fn=fn, args=args, kwargs=kwargs, num_returns=num_returns)
        if num_returns > 1:
            return tuple(FakeObjectRef(task=task, index=i) for i in range(num_returns))
        return FakeObjectRef(task=task, index=0)

    def submit_with_status(self, fn, *args, num_returns=1, **kwargs):
        """Return (payload_ref_or_tuple, status_ref) from the same task."""
        self.submissions.append((fn.__name__, {k: type(v).__name__ for k, v in kwargs.items()}))
        task = FakeTask(fn=fn, args=args, kwargs=kwargs, num_returns=num_returns)
        payload_refs = tuple(FakeObjectRef(task=task, index=i) for i in range(num_returns))
        status_ref = FakeObjectRef(task=task, index=num_returns, is_status=True)
        if num_returns == 1:
            return payload_refs[0], status_ref
        return payload_refs, status_ref

    def submit_with_services(
        self,
        fn,
        execution_services,
        task,
        input_type,
        run_input,
        input_param_names,
        receipt_dependencies,
        num_returns,
        /,
        *args,
        **kwargs,
    ):
        from runtime.executor import _distributed_execution_services_task

        refs = self.submit(
            _distributed_execution_services_task,
            fn,
            execution_services,
            task,
            input_type,
            run_input,
            input_param_names,
            len(receipt_dependencies),
            num_returns,
            *receipt_dependencies,
            *args,
            num_returns=num_returns + 2,
            **kwargs,
        )
        refs = tuple(refs)
        receipt_ref = refs[-2]
        receipt_ref.is_receipt = True
        status_ref = refs[-1]
        status_ref.is_status = True
        if num_returns == 1:
            return refs[0], receipt_ref, status_ref
        return refs[:-2], receipt_ref, status_ref

    # -- object store -----------------------------------------------------

    def put(self, value) -> FakeObjectRef:
        return FakeObjectRef(is_stored=True, stored_label=self._context, stored_value=value)

    def is_ref(self, value) -> bool:
        return isinstance(value, FakeObjectRef)

    def worker_get(self, value):
        """Worker-side dereference (models ``stream._ray_get`` inside a task).

        Resolves refs via the object store WITHOUT touching
        ``driver_payload_gets`` — that is the whole point: the frame moves
        worker-side, not through the driver. Records to ``worker_payload_gets``.
        """
        if isinstance(value, FakeObjectRef):
            self.worker_payload_gets.append(self._task_label(value))
            return self._resolve(value, context="stream_wrapper")
        return value

    def _normalize_worker_result(self, value):
        """Model the Ray task wrapper's control/data split for AppendResult.

        A task that returns an ``AppendResult`` has its frame placed in the
        object store and only a small ``AppendResultHandle`` travels as the
        payload — so the driver can inspect the handle without the frame.
        """
        if isinstance(value, LineagedResult):
            return LineagedResult(
                self._normalize_worker_result(value.value),
                dict(value.lineage_vector),
            )
        if isinstance(value, AppendResult):
            return AppendResultHandle(
                data_ref=self.put(value.data),
                snapshot_id=value.snapshot_id,
                table_identity=value.table_identity,
            )
        if isinstance(value, tuple):
            return tuple(self._normalize_worker_result(v) for v in value)
        if isinstance(value, list):
            return [self._normalize_worker_result(v) for v in value]
        if isinstance(value, dict):
            return {k: self._normalize_worker_result(v) for k, v in value.items()}
        return value

    def project(self, ref, index):
        """Worker-side projection: submit a task that returns ``ref[index]``.

        Inspecting the payload inside a projection task is legitimate data
        movement (the tuple must be opened somewhere); it is NOT a driver fetch.
        """

        def _project_index(value):
            from avalanche.types import LineagedResult

            if isinstance(value, LineagedResult):
                return LineagedResult(value.value[index], dict(value.lineage_vector))
            return value[index]

        return self.submit(_project_index, ref)

    # -- readiness (no materialization) -----------------------------------

    def wait(self, futures) -> None:
        remaining = list(futures)
        while remaining:
            _ready, remaining = self.ray.wait(remaining, num_returns=1)

    # -- driver fetch -----------------------------------------------------

    def get(self, futures):
        out = []
        for ref in futures:
            value = self._resolve(ref, context="__driver__")
            if isinstance(ref, FakeObjectRef) and ref.is_status:
                self.status_gets.append(self._task_label(ref))
                out.append(value)
            elif isinstance(ref, FakeObjectRef) and ref.is_receipt:
                self.receipt_gets.append(self._task_label(ref))
                out.append(value)
            else:
                if isinstance(ref, FakeObjectRef):
                    self.driver_payload_gets.append(self._task_label(ref))
                out.append(value)
        return out

    # -- resolution -------------------------------------------------------

    def _resolve(self, ref, *, context: str):
        if not isinstance(ref, FakeObjectRef):
            return ref
        if ref.is_stored:
            # Data-plane payload dereference — real data movement.
            self.payload_resolutions.append((context, ref.stored_label or "?"))
            return ref.stored_value
        task = ref.task
        assert task is not None
        self._run_task(task, context=context)
        if task.error is not None:
            raise task.error
        if ref.is_status:
            return None
        return task.outputs[ref.index if ref.index is not None else 0]

    def _run_task(self, task: FakeTask, *, context: str):
        if task.computed:
            return
        prev = self._context
        self._context = task.fn.__name__ if task.fn else context
        try:
            args = tuple(self._resolve(a, context=self._context) for a in task.args)
            kwargs = {
                k: self._resolve(v, context=self._context) for k, v in task.kwargs.items()
            }
            try:
                result = task.fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - re-raised on fetch
                task.error = exc
                task.computed = True
                return
            if task.num_returns > 1:
                task.outputs = tuple(self._normalize_worker_result(v) for v in result)
            else:
                task.outputs = (self._normalize_worker_result(result),)
            task.computed = True
        finally:
            self._context = prev

    def _task_label(self, ref: FakeObjectRef) -> str:
        if ref.task is not None and ref.task.fn is not None:
            name = ref.task.fn.__name__
            if (
                ref.task.num_returns > 1
                and ref.index is not None
                and not ref.is_status
                and not ref.is_receipt
            ):
                return f"{name}[{ref.index}]"
            return name
        if ref.is_stored:
            return ref.stored_label or "?"
        return "?"

    def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _assert_no_driver_payload(executor: RayExecutor):
    assert (
        executor.driver_payload_gets == []
    ), f"driver materialized payloads: {executor.driver_payload_gets}"


def _assert_no_synthetic_payload_resolution(executor: RayExecutor):
    synthetic = [
        (ctx, label)
        for ctx, label in executor.payload_resolutions
        if ctx in ("__driver__", "__status__", "__projection__")
    ]
    assert synthetic == [], f"synthetic path resolved payloads: {synthetic}"


def test_execution_service_receipts_do_not_materialize_user_payloads_on_driver():
    from execution_services_test import RecordingServices, _raw_input, _spec

    class Input(ava.BaseInput):
        scalar: str
        values: list[str]
        optional: str | None
        empty: list[str]
        worker_pid: int

    @ava.source
    def load(value: str):
        return [value] * 100

    @ava.step
    def left(value):
        return value + ["left"]

    @ava.step
    def right(value):
        return value + ["right"]

    @ava.step
    def join(left_value, right_value):
        return len(left_value) + len(right_value)

    @ava.workflow(input=Input)
    def flow():
        root = load(ava.input.scalar)
        return join(left(root), right(root))

    executor = RayExecutor()
    handle = flow().run(
        executor=executor,
        input=_raw_input(),
        execution_services=_spec(RecordingServices()),
    )

    assert handle.result() == 202
    assert [receipt.node_id for receipt in handle.execution_receipts()] == ["join_1"]
    assert executor.driver_payload_gets == ["_distributed_execution_services_task[0]"]
    assert len(executor.status_gets) == 4
    assert len(executor.receipt_gets) == 1
    assert all(context != "__driver__" for context, _label in executor.payload_resolutions)


# ---------------------------------------------------------------------------
# 1. final-return, no hooks: good behavior preserved
# ---------------------------------------------------------------------------


def test_final_return_no_hooks_only_fetches_terminal():
    @ava.source
    def load():
        return [1, 2, 3]

    @ava.step
    def double(xs):
        return [x * 2 for x in xs]

    @ava.dest
    def total(xs):
        return sum(xs)

    @ava.workflow
    def wf():
        return load() >> double() >> total()

    executor = RayExecutor()
    result = wf().run(executor=executor).result()

    assert result == 12
    downstream = [s for s in executor.submissions if s[0] in ("double", "total")]
    for _name, kwargs in downstream:
        assert "FakeObjectRef" in kwargs.values()
    assert executor.driver_payload_gets == ["total"], executor.driver_payload_gets


# ---------------------------------------------------------------------------
# 2. no-return workflow
# ---------------------------------------------------------------------------


def test_no_return_workflow_does_not_materialize_payloads():
    @ava.source
    def load():
        return [1, 2, 3]

    @ava.step
    def double(xs):
        return [x * 2 for x in xs]

    @ava.dest
    def sink(xs):
        return sum(xs)

    @ava.workflow
    def wf():
        load() >> double() >> sink()

    executor = RayExecutor()
    result = wf().run(executor=executor).result()

    assert result is None
    _assert_no_driver_payload(executor)
    _assert_no_synthetic_payload_resolution(executor)
    assert (
        executor.ray.wait_calls or executor.status_gets
    ), "no-return workflow never observed completion"


def test_no_return_workflow_surfaces_task_failure():
    @ava.source
    def boom():
        raise ValueError("kaboom")

    @ava.dest
    def sink(x):
        return x

    @ava.workflow
    def wf():
        boom() >> sink()

    executor = RayExecutor()
    try:
        wf().run(executor=executor).result()
    except ValueError as exc:
        assert "kaboom" in str(exc)
    else:
        raise AssertionError("no-return workflow swallowed a task failure")


# ---------------------------------------------------------------------------
# 3. success hooks
# ---------------------------------------------------------------------------


def test_success_hook_does_not_materialize_payloads():
    @ava.source
    def left():
        return "left-payload"

    @ava.source
    def right():
        return "right-payload"

    @ava.workflow
    def wf():
        left()
        right()

    events: list[str] = []
    executor = RayExecutor()
    wf().run(executor=executor, hooks=RunHooks(on_node_success=events.append)).result()

    assert set(events) == {"left_1", "right_1"}
    _assert_no_driver_payload(executor)
    _assert_no_synthetic_payload_resolution(executor)


def test_success_hook_with_return_fetches_only_final_once():
    @ava.source
    def load():
        return [1, 2, 3]

    @ava.dest
    def total(xs):
        return sum(xs)

    @ava.workflow
    def wf():
        return load() >> total()

    events: list[str] = []
    executor = RayExecutor()
    result = wf().run(executor=executor, hooks=RunHooks(on_node_success=events.append)).result()

    assert result == 6
    assert "load" not in executor.driver_payload_gets, executor.driver_payload_gets
    assert executor.driver_payload_gets.count("total") <= 1, executor.driver_payload_gets
    _assert_no_synthetic_payload_resolution(executor)


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_cancelled_drain_does_not_swallow_driver_hook_base_exceptions(exception_type):
    @ava.source
    def load():
        return "payload"

    @ava.dest
    def sink(value):
        return value

    @ava.workflow
    def wf():
        return load() >> sink()

    cancel_checks = 0

    def cancel_after_root_submission():
        nonlocal cancel_checks
        cancel_checks += 1
        return cancel_checks >= 2

    def raise_from_hook(_node_id):
        raise exception_type("hook-interrupt")

    handle = wf().run(
        executor=RayExecutor(),
        hooks=RunHooks(
            on_node_success=raise_from_hook,
            cancel_requested=cancel_after_root_submission,
        ),
    )

    with pytest.raises(exception_type, match="hook-interrupt"):
        handle.result()


# ---------------------------------------------------------------------------
# 4. Stream passthrough
# ---------------------------------------------------------------------------


class _DummyTable:
    identifier = "ns.dummy"
    location = "mem://ns/dummy"
    row_lineage = True


@contextlib.contextmanager
def _fake_consume_stream(table, key=None, *, mode="run_scoped", upstream_data=None, **kw):
    if isinstance(upstream_data, AppendResult):
        yield upstream_data.to_polars()
    elif isinstance(upstream_data, pl.DataFrame):
        yield upstream_data
    else:
        yield pl.DataFrame({"x": []})


def test_stream_passthrough_does_not_materialize_on_driver(monkeypatch):
    monkeypatch.setattr(stream_mod, "consume_stream", _fake_consume_stream)

    executor = RayExecutor()

    # Worker-side dereference: model _ray_get inside the consumer task. It must
    # resolve refs via the fake object store WITHOUT touching driver_payload_gets
    # (that is the whole point — the frame moves worker-side, not through the
    # driver). Records to worker_payload_gets so we can assert it DID happen.
    monkeypatch.setattr(stream_mod, "_ray_get", executor.worker_get)

    @ava.source
    def produce():
        return AppendResult(data=pl.DataFrame({"x": [1, 2, 3]}), snapshot_id=7)

    @ava.step
    def consume(df=ava.Stream(_DummyTable())):
        return df.height

    @ava.workflow
    def wf():
        return produce() >> consume()

    result = wf().run(executor=executor).result()

    assert result == 3
    # The driver must not fetch the producer's payload just to detect
    # passthrough. It may inspect a small control handle worker-side; the frame
    # is dereferenced inside the consumer task, never on the driver.
    assert "produce" not in executor.driver_payload_gets, (
        f"driver materialized Stream parent for passthrough detection: "
        f"{executor.driver_payload_gets}"
    )


# ---------------------------------------------------------------------------
# 5. true multi-return indexing
# ---------------------------------------------------------------------------


def test_true_multireturn_index_passes_selected_ref():
    @ava.source(num_returns=2)
    def split():
        return "left", "right"

    @ava.dest
    def sink(x):
        return f"got:{x}"

    @ava.workflow
    def wf():
        pair = split()
        return pair[0] >> sink()

    executor = RayExecutor()
    result = wf().run(executor=executor).result()

    assert result == "got:left"
    assert "split[1]" not in executor.driver_payload_gets, executor.driver_payload_gets


def test_true_multireturn_index_explicit_arg_passes_selected_ref():
    @ava.source(num_returns=2)
    def split():
        return "left", "right"

    @ava.dest
    def sink(x):
        return f"got:{x}"

    @ava.workflow
    def wf():
        pair = split()
        return sink(pair[0])

    executor = RayExecutor()
    result = wf().run(executor=executor).result()

    assert result == "got:left"
    assert "split[1]" not in executor.driver_payload_gets, executor.driver_payload_gets


@pytest.mark.parametrize("binding_style", ["explicit", "chain"])
def test_true_multireturn_indexed_stream_selector_stays_off_driver(
    monkeypatch,
    binding_style,
):
    monkeypatch.setattr(stream_mod, "consume_stream", _fake_consume_stream)

    executor = RayExecutor()
    monkeypatch.setattr(stream_mod, "_ray_get", executor.worker_get)

    @ava.source(num_returns=2)
    def split():
        return (
            AppendResult(
                data=pl.DataFrame({"x": ["left"]}),
                snapshot_id=1,
                table_identity="ns.dummy",
            ),
            AppendResult(
                data=pl.DataFrame({"x": ["right"]}),
                snapshot_id=2,
                table_identity="ns.dummy",
            ),
        )

    @ava.step
    def consume(df=ava.Stream(_DummyTable())):
        return df["x"][0]

    @ava.workflow
    def wf():
        pair = split()
        if binding_style == "explicit":
            return consume(pair[1], df=ava.Stream(_DummyTable()))
        return pair[1] >> consume(df=ava.Stream(_DummyTable()))

    assert wf().run(executor=executor).result() == "right"
    assert "split[0]" not in executor.driver_payload_gets
    assert "split[1]" not in executor.driver_payload_gets
    assert executor.worker_payload_gets == ["split"]
    _assert_no_synthetic_payload_resolution(executor)


def test_varargs_stream_selector_reconstructs_slots_without_driver_fetch(monkeypatch):
    monkeypatch.setattr(stream_mod, "consume_stream", _fake_consume_stream)

    executor = RayExecutor()
    monkeypatch.setattr(stream_mod, "_ray_get", executor.worker_get)

    @ava.source(num_returns=2)
    def split():
        return (
            AppendResult(
                data=pl.DataFrame({"x": ["left"]}),
                snapshot_id=1,
                table_identity="ns.dummy",
            ),
            AppendResult(
                data=pl.DataFrame({"x": ["right"]}),
                snapshot_id=2,
                table_identity="ns.dummy",
            ),
        )

    @ava.step
    def consume(prefix, df=ava.Stream(_DummyTable()), *tail):
        return prefix, df["x"][0], tail

    @ava.workflow
    def wf():
        pair = split()
        return consume("pre", pair[1], "post")

    seen_worker_args: list[tuple[Any, ...]] = []

    def wrap_fn(node_id, fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if node_id == "consume_1":
                seen_worker_args.append(args)
            return fn(*args, **kwargs)

        return wrapped

    assert wf().run(
        executor=executor,
        hooks=RunHooks(wrap_fn=wrap_fn),
    ).result() == ("pre", "right", ("post",))
    assert len(seen_worker_args) == 1
    assert seen_worker_args[0][0] == "pre"
    assert seen_worker_args[0][1]["x"].to_list() == ["right"]
    assert seen_worker_args[0][2:] == ("post",)
    assert "split[0]" not in executor.driver_payload_gets
    assert "split[1]" not in executor.driver_payload_gets
    assert executor.worker_payload_gets == ["split"]
    _assert_no_synthetic_payload_resolution(executor)


def test_positional_only_injected_slots_reconstruct_without_driver_fetch(monkeypatch):
    monkeypatch.setattr(stream_mod, "consume_stream", _fake_consume_stream)

    executor = RayExecutor()
    monkeypatch.setattr(stream_mod, "_ray_get", executor.worker_get)

    @ava.source(num_returns=2)
    def split():
        return (
            AppendResult(
                data=pl.DataFrame({"value": ["left"]}),
                snapshot_id=1,
                table_identity="ns.dummy",
            ),
            AppendResult(
                data=pl.DataFrame({"value": ["right"]}),
                snapshot_id=2,
                table_identity="ns.dummy",
            ),
        )

    consume = ava.step(positional_only_selector_consume)

    @ava.workflow(input=RerunSelectorInput)
    def wf():
        pair = split()
        return pair[1] >> consume(df=ava.Stream(_DummyTable()))

    seen_worker_args: list[tuple[Any, ...]] = []

    def wrap_fn(node_id, fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if node_id == "positional_only_selector_consume_1":
                seen_worker_args.append(args)
            return fn(*args, **kwargs)

        return wrapped

    assert wf().run(
        executor=executor,
        hooks=RunHooks(wrap_fn=wrap_fn),
        input={"suffix": "!"},
    ).result() == ("right", "!")
    assert len(seen_worker_args) == 1
    assert isinstance(seen_worker_args[0][0], RerunSelectorInput)
    assert seen_worker_args[0][1]["value"].to_list() == ["right"]
    assert "split[0]" not in executor.driver_payload_gets
    assert "split[1]" not in executor.driver_payload_gets
    assert executor.worker_payload_gets == ["split"]
    _assert_no_synthetic_payload_resolution(executor)


def test_implicit_varargs_reconstructs_positional_only_provider_slots(monkeypatch):
    monkeypatch.setattr(stream_mod, "consume_stream", _fake_consume_stream)

    executor = RayExecutor()
    monkeypatch.setattr(stream_mod, "_ray_get", executor.worker_get)

    @ava.source
    def prefix():
        return "pre"

    @ava.source
    def produce():
        return AppendResult(
            data=pl.DataFrame({"x": ["right"]}),
            snapshot_id=1,
            table_identity="ns.dummy",
        )

    @ava.source
    def tail():
        return "post"

    @ava.step
    def consume(prefix, df=ava.Stream(_DummyTable()), /, *tail):
        return prefix, df["x"][0], tail

    @ava.workflow
    def wf():
        return (prefix() & produce() & tail()) >> consume()

    assert wf().run(executor=executor).result() == ("pre", "right", ("post",))
    assert "produce" not in executor.driver_payload_gets
    assert executor.worker_payload_gets == ["produce"]
    _assert_no_synthetic_payload_resolution(executor)


def test_unindexed_multireturn_expands_before_mixed_stream_slot(monkeypatch):
    monkeypatch.setattr(stream_mod, "consume_stream", _fake_consume_stream)

    executor = RayExecutor()
    monkeypatch.setattr(stream_mod, "_ray_get", executor.worker_get)

    @ava.source(num_returns=2)
    def split():
        return (
            "left",
            AppendResult(
                data=pl.DataFrame({"x": ["middle"]}),
                snapshot_id=1,
                table_identity="ns.dummy",
            ),
        )

    @ava.source
    def other():
        return "other"

    @ava.step
    def consume(left, middle, right):
        return left, middle["x"][0], right

    @ava.workflow
    def wf():
        return (split() & other()) >> consume(middle=ava.Stream(_DummyTable()))

    assert wf().run(executor=executor).result() == ("left", "middle", "other")
    assert "split[0]" not in executor.driver_payload_gets
    assert "split[1]" not in executor.driver_payload_gets
    assert executor.worker_payload_gets == ["split"]
    _assert_no_synthetic_payload_resolution(executor)


def test_keyword_only_chained_stream_selector_stays_off_driver(monkeypatch):
    monkeypatch.setattr(stream_mod, "consume_stream", _fake_consume_stream)

    executor = RayExecutor()
    monkeypatch.setattr(stream_mod, "_ray_get", executor.worker_get)

    @ava.source(num_returns=2)
    def split():
        return (
            AppendResult(
                data=pl.DataFrame({"x": ["left"]}),
                snapshot_id=1,
                table_identity="ns.dummy",
            ),
            AppendResult(
                data=pl.DataFrame({"x": ["right"]}),
                snapshot_id=2,
                table_identity="ns.dummy",
            ),
        )

    @ava.step
    def consume(*, df=ava.Stream(_DummyTable())):
        return df["x"][0]

    @ava.workflow
    def wf():
        pair = split()
        return pair[1] >> consume(df=ava.Stream(_DummyTable()))

    assert wf().run(executor=executor).result() == "right"
    assert "split[0]" not in executor.driver_payload_gets
    assert "split[1]" not in executor.driver_payload_gets
    assert executor.worker_payload_gets == ["split"]
    _assert_no_synthetic_payload_resolution(executor)


def test_parallel_true_multireturn_stream_selectors_stay_off_driver(monkeypatch):
    monkeypatch.setattr(stream_mod, "consume_stream", _fake_consume_stream)

    executor = RayExecutor()
    monkeypatch.setattr(stream_mod, "_ray_get", executor.worker_get)

    @ava.source(num_returns=2)
    def split():
        return (
            AppendResult(
                data=pl.DataFrame({"x": ["left"]}),
                snapshot_id=1,
                table_identity="ns.dummy",
            ),
            AppendResult(
                data=pl.DataFrame({"x": ["right"]}),
                snapshot_id=2,
                table_identity="ns.dummy",
            ),
        )

    @ava.step
    def consume(left_df, right_df):
        return f"{left_df['x'][0]}+{right_df['x'][0]}"

    @ava.workflow
    def wf():
        pair = split()
        return (pair[1] & pair[0]) >> consume(
            left_df=ava.Stream(_DummyTable()),
            right_df=ava.Stream(_DummyTable()),
        )

    assert wf().run(executor=executor).result() == "right+left"
    assert "split[0]" not in executor.driver_payload_gets
    assert "split[1]" not in executor.driver_payload_gets
    assert executor.worker_payload_gets == ["split", "split"]
    _assert_no_synthetic_payload_resolution(executor)


# ---------------------------------------------------------------------------
# 6. single-return tuple indexing
# ---------------------------------------------------------------------------


def test_single_return_tuple_index_uses_remote_projection():
    @ava.source
    def split():
        return ("left", "right")

    @ava.dest
    def sink(x):
        return f"got:{x}"

    @ava.workflow
    def wf():
        pair = split()
        return pair[0] >> sink()

    executor = RayExecutor()
    result = wf().run(executor=executor).result()

    assert result == "got:left"
    assert "split" not in executor.driver_payload_gets, (
        f"driver materialized single-return tuple to index it: "
        f"{executor.driver_payload_gets}"
    )
