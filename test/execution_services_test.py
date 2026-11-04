from __future__ import annotations

import os
from concurrent.futures import CancelledError
from dataclasses import dataclass
from typing import Any

import pytest

import avalanche as ava
from runtime.operator.hooks import RunHooks


@dataclass(frozen=True)
class ManagedValue:
    value: str


@dataclass(frozen=True)
class ServiceRequest:
    run_key: str


class ManagedInput(ava.BaseInput):
    scalar: str
    values: list[str]
    optional: str | None
    empty: list[str]
    worker_pid: int


class RecordingServices:
    def __init__(self, events: list[tuple[Any, ...]] | None = None):
        self.events = events if events is not None else []

    def probe(self, *, request, task):
        self.events.append((task.node_id, "probe"))
        return f"probe:{request.run_key}"

    def negotiate(self, *, request, task, probe):
        self.events.append((task.node_id, "negotiate", probe))
        return f"negotiated:{request.run_key}"

    def open(self, *, request, task, negotiation, upstream_receipts):
        self.events.append((task.node_id, "open", negotiation, tuple(upstream_receipts)))
        return {"request": request, "task": task}

    def materialize_input(self, *, session, input_type, input):
        task = session["task"]
        self.events.append((task.node_id, "materialize"))
        return {
            "scalar": input["scalar"].value,
            "values": [item.value for item in input["values"]],
            "optional": (None if input["optional"] is None else input["optional"].value),
            "empty": [item.value for item in input["empty"]],
            "worker_pid": os.getpid(),
        }

    def finalize(self, *, session):
        task = session["task"]
        self.events.append((task.node_id, "finalize"))
        return {"node": task.node_id, "pid": os.getpid()}

    def abort(self, *, session, error):
        task = session["task"]
        self.events.append((task.node_id, "abort", type(error).__name__))

    def teardown(self, *, session):
        task = session["task"]
        self.events.append((task.node_id, "teardown"))


def _spec(service: Any) -> ava.ExecutionServicesSpec:
    return ava.ExecutionServicesSpec(
        service=service,
        request=ServiceRequest(run_key="run-descriptor"),
    )


def _raw_input() -> dict[str, Any]:
    # These descriptors are deliberately invalid ManagedInput field values.
    # Driver-side model construction would fail before the service can materialize them.
    return {
        "scalar": ManagedValue("scalar"),
        "values": [ManagedValue("first"), ManagedValue("second"), ManagedValue("first")],
        "optional": None,
        "empty": [],
    }


@pytest.mark.parametrize("max_workers", (None, 1), ids=("default", "serial"))
def test_local_service_input_lifecycle_receipts_and_fan_in(max_workers: int | None):
    events: list[tuple[Any, ...]] = []
    services = RecordingServices(events)

    @ava.source
    def typed(payload: ManagedInput, ctx: ava.RunContext):
        return payload.scalar, payload.values, payload.optional, payload.empty

    @ava.source
    def selected(scalar: str, values: list[str], optional: str | None):
        return scalar, values, optional

    @ava.step
    def join(left, right):
        return left, right

    @ava.workflow(input=ManagedInput)
    def flow():
        left = typed()
        right = selected(ava.input.scalar, ava.input.values, ava.input.optional)
        return join(left, right)

    handle = flow().run(
        executor=ava.LocalExecutor(max_workers=max_workers),
        input=_raw_input(),
        execution_services=_spec(services),
    )

    expected = ("scalar", ["first", "second", "first"], None, [])
    assert handle.result(timeout=5) == (expected, expected[:3])
    receipts = handle.execution_receipts(timeout=5)
    assert [(receipt.node_id, receipt.node_slug) for receipt in receipts] == [
        ("join_1", "join")
    ]
    assert receipts[0].value["node"] == "join_1"

    opens = [event for event in events if event[1] == "open"]
    assert {event[0] for event in opens[:2]} == {"typed_1", "selected_1"}
    assert opens[2][0] == "join_1"
    assert all(event[3] == () for event in opens[:2])
    assert [receipt["node"] for receipt in opens[2][3]] == ["typed_1", "selected_1"]
    for node_id in ("typed_1", "selected_1", "join_1"):
        assert [event[1] for event in events if event[0] == node_id] == [
            "probe",
            "negotiate",
            "open",
            "materialize",
            "finalize",
            "teardown",
        ]


@pytest.mark.parametrize("failure_stage", ["materialize", "user", "finalize"])
def test_opened_failure_aborts_and_tears_down_once(failure_stage):
    events: list[tuple[Any, ...]] = []

    class FailingServices(RecordingServices):
        def materialize_input(self, **kwargs):
            if failure_stage == "materialize":
                self.events.append((kwargs["session"]["task"].node_id, "materialize"))
                raise RuntimeError("materialize failed")
            return super().materialize_input(**kwargs)

        def finalize(self, **kwargs):
            if failure_stage == "finalize":
                self.events.append((kwargs["session"]["task"].node_id, "finalize"))
                raise RuntimeError("finalize failed")
            return super().finalize(**kwargs)

    called = []

    @ava.source
    def task(payload: ManagedInput):
        called.append(payload.scalar)
        if failure_stage == "user":
            raise RuntimeError("user failed")
        return payload.scalar

    @ava.workflow(input=ManagedInput)
    def flow():
        return task()

    handle = flow().run(
        executor=ava.LocalExecutor(),
        input=_raw_input(),
        execution_services=_spec(FailingServices(events)),
    )
    with pytest.raises(RuntimeError, match=f"{failure_stage} failed"):
        handle.result(timeout=5)
    with pytest.raises(RuntimeError, match=f"{failure_stage} failed"):
        handle.execution_receipts(timeout=5)
    assert sum(event[1] == "abort" for event in events) == 1
    assert sum(event[1] == "teardown" for event in events) == 1
    assert called == ([] if failure_stage == "materialize" else ["scalar"])


def test_open_failure_has_no_fallback_or_opened_session_cleanup():
    events: list[tuple[Any, ...]] = []

    class OpenFailure(RecordingServices):
        def open(self, **kwargs):
            self.events.append((kwargs["task"].node_id, "open"))
            raise RuntimeError("open failed")

    called = []

    @ava.source
    def task(value: str):
        called.append(value)

    @ava.workflow(input=ManagedInput)
    def flow():
        return task(ava.input.scalar)

    handle = flow().run(
        executor=ava.LocalExecutor(),
        input=_raw_input(),
        execution_services=_spec(OpenFailure(events)),
    )
    with pytest.raises(RuntimeError, match="open failed"):
        handle.result(timeout=5)
    assert called == []
    assert [event[1] for event in events] == ["probe", "negotiate", "open"]


def test_malformed_multi_return_aborts_before_finalize():
    events: list[tuple[Any, ...]] = []

    @ava.source(num_returns=2)
    def task(payload: ManagedInput):
        return (payload.scalar,)

    @ava.workflow(input=ManagedInput)
    def flow():
        return task()

    handle = flow().run(
        executor=ava.LocalExecutor(),
        input=_raw_input(),
        execution_services=_spec(RecordingServices(events)),
    )
    with pytest.raises(ValueError, match="expected to return 2 values"):
        handle.result(timeout=5)
    with pytest.raises(ValueError, match="expected to return 2 values"):
        handle.execution_receipts(timeout=5)
    assert [event[1] for event in events] == [
        "probe",
        "negotiate",
        "open",
        "materialize",
        "abort",
        "teardown",
    ]


def test_cancellation_preserves_completed_service_lifecycle_and_skips_downstream():
    events: list[tuple[Any, ...]] = []
    starts: list[str] = []

    @ava.source
    def first(payload: ManagedInput):
        return payload.scalar

    @ava.step
    def second(value: str):
        return value

    @ava.workflow(input=ManagedInput)
    def flow():
        return first() >> second()

    handle = flow().run(
        executor=ava.LocalExecutor(),
        hooks=RunHooks(
            on_node_start=starts.append,
            cancel_requested=lambda: len(starts) >= 1,
        ),
        input=_raw_input(),
        execution_services=_spec(RecordingServices(events)),
    )
    with pytest.raises(CancelledError):
        handle.result(timeout=5)
    assert starts == ["first_1"]
    assert [event[0] for event in events if event[1] == "open"] == ["first_1"]
    assert [event[1] for event in events if event[0] == "first_1"][-2:] == [
        "finalize",
        "teardown",
    ]


def test_cleanup_failures_do_not_mask_primary_task_failure():
    class CleanupFailureServices(RecordingServices):
        def abort(self, *, session, error):
            raise RuntimeError("abort cleanup failed")

        def teardown(self, *, session):
            raise RuntimeError("teardown cleanup failed")

    @ava.source
    def fail(payload: ManagedInput):
        raise ValueError("primary task failed")

    @ava.workflow(input=ManagedInput)
    def flow():
        return fail()

    handle = flow().run(
        executor=ava.LocalExecutor(),
        input=_raw_input(),
        execution_services=_spec(CleanupFailureServices()),
    )
    with pytest.raises(ValueError, match="primary task failed") as error:
        handle.result(timeout=5)
    assert any("abort cleanup failed" in note for note in error.value.__notes__)
    assert any("teardown cleanup failed" in note for note in error.value.__notes__)
