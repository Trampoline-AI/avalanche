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
    driver_pid: int = 0


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


class MetadataCollisionRayServices:
    def probe(self, *, request, task):
        return request.run_key

    def negotiate(self, *, request, task, probe):
        return probe

    def open(self, *, request, task, negotiation, upstream_receipts):
        return task

    def materialize_input(self, *, session, input_type, input):
        return input

    def finalize(self, *, session):
        return {"receipt-node": session.node_id}

    def abort(self, *, session, error):
        return None

    def teardown(self, *, session):
        return None


def capture_kwargs(**kwargs):
    return kwargs


ALL_INTERNAL_METADATA_KWARGS = {
    name: f"user-{name.replace('_', '-')}"
    for name in (
        "fn",
        "execution_services",
        "task",
        "input_type",
        "run_input",
        "input_param_names",
        "receipt_dependencies",
        "num_returns",
        "dependency_count",
        "user_num_returns",
        "dependency_and_user_args",
        "spec",
        "raw_input",
        "upstream_receipts",
        "args",
        "kwargs",
        "normalize_result",
        "context",
        "request",
        "probe",
        "negotiation",
        "session",
        "input",
        "error",
        "receipt",
        "internal_metadata",
    )
}


class RayLifecycleEventRecorder:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)

    def read(self):
        return self.events


class MalformedReturnRayServices:
    def __init__(self, recorder):
        self.recorder = recorder

    def record(self, event):
        import ray

        ray.get(self.recorder.append.remote(event))

    def probe(self, *, request, task):
        self.record("probe")
        return request.run_key

    def negotiate(self, *, request, task, probe):
        self.record("negotiate")
        return probe

    def open(self, *, request, task, negotiation, upstream_receipts):
        self.record("open")
        return task

    def materialize_input(self, *, session, input_type, input):
        self.record("materialize")
        return input

    def finalize(self, *, session):
        self.record("finalize")
        return session.node_id

    def abort(self, *, session, error):
        self.record("abort")

    def teardown(self, *, session):
        self.record("teardown")


def return_value(value):
    return value


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


def test_local_service_input_lifecycle_receipts_and_fan_in():
    events: list[tuple[Any, ...]] = []
    services = RecordingServices(events)

    @ava.source
    def typed(payload: ManagedInput, ctx: ava.RunContext):
        assert "execution_services" not in ctx.model_dump()
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
        executor=ava.LocalExecutor(),
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


def test_result_normalization_failure_aborts_before_finalize_and_tears_down_once():
    from avalanche.execution_services import _run_with_execution_services

    events: list[tuple[Any, ...]] = []
    task_spec = ava.ExecutionTaskSpec(
        run_id="run",
        workflow_name="flow",
        node_id="task_1",
        node_name="task",
        node_slug="task",
        executor_type="local",
    )

    def task(payload: ManagedInput):
        return payload.scalar

    def fail_normalization(_result):
        raise RuntimeError("normalize failed")

    with pytest.raises(RuntimeError, match="normalize failed"):
        _run_with_execution_services(
            task,
            _spec(RecordingServices(events)),
            task_spec,
            ManagedInput,
            _raw_input(),
            ("payload",),
            (),
            (),
            {},
            num_returns=1,
            normalize_result=fail_normalization,
        )

    assert [event[1] for event in events] == [
        "probe",
        "negotiate",
        "open",
        "materialize",
        "abort",
        "teardown",
    ]


@pytest.mark.parametrize(
    "malformed_result",
    [(), ("only",), ("one", "two", "three")],
    ids=["zero-values", "one-value", "wrong-multi-value-count"],
)
def test_local_malformed_multi_return_aborts_before_finalize_and_tears_down_once(
    malformed_result,
):
    events: list[tuple[Any, ...]] = []

    @ava.source(num_returns=2)
    def task(payload: ManagedInput):
        return malformed_result

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


def test_retry_restarts_the_complete_worker_lifecycle():
    events: list[tuple[Any, ...]] = []

    class RetryingExecutor(ava.LocalExecutor):
        def submit_with_services(self, fn, *args, **kwargs):
            try:
                return super().submit_with_services(fn, *args, **kwargs)
            except RuntimeError as error:
                if str(error) != "retry me":
                    raise
                return super().submit_with_services(fn, *args, **kwargs)

    attempts = 0

    @ava.source
    def task(payload: ManagedInput):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("retry me")
        return payload.scalar

    @ava.workflow(input=ManagedInput)
    def flow():
        return task()

    handle = flow().run(
        executor=RetryingExecutor(),
        input=_raw_input(),
        execution_services=_spec(RecordingServices(events)),
    )
    assert handle.result(timeout=5) == "scalar"
    assert [event[1] for event in events].count("open") == 2
    assert [event[1] for event in events].count("abort") == 1
    assert [event[1] for event in events].count("finalize") == 1
    assert [event[1] for event in events].count("teardown") == 2


def test_malformed_return_retry_restarts_the_complete_guarded_lifecycle():
    events: list[tuple[Any, ...]] = []

    class RetryingExecutor(ava.LocalExecutor):
        def submit_with_services(self, fn, *args, **kwargs):
            try:
                return super().submit_with_services(fn, *args, **kwargs)
            except ValueError as error:
                if "expected to return 2 values" not in str(error):
                    raise
                return super().submit_with_services(fn, *args, **kwargs)

    attempts = 0

    @ava.source(num_returns=2)
    def task(payload: ManagedInput):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return (payload.scalar,)
        return payload.scalar, "ok"

    @ava.workflow(input=ManagedInput)
    def flow():
        return task()

    handle = flow().run(
        executor=RetryingExecutor(),
        input=_raw_input(),
        execution_services=_spec(RecordingServices(events)),
    )
    assert handle.result(timeout=5) == ("scalar", "ok")
    assert [event[1] for event in events] == [
        "probe",
        "negotiate",
        "open",
        "materialize",
        "abort",
        "teardown",
        "probe",
        "negotiate",
        "open",
        "materialize",
        "finalize",
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


def test_version_and_protocol_validation_and_unchanged_empty_receipts():
    with pytest.raises(ValueError, match="Unsupported execution services version"):
        ava.ExecutionServicesSpec(
            service=RecordingServices(),
            request=ServiceRequest("run"),
            version="future",
        )
    with pytest.raises(TypeError, match="ExecutionServices protocol"):
        ava.ExecutionServicesSpec(service=object(), request=ServiceRequest("run"))

    @ava.workflow
    def unchanged():
        return "ordinary"

    handle = unchanged().run(executor=ava.LocalExecutor())
    assert handle.result(timeout=5) == "ordinary"
    assert handle.execution_receipts(timeout=5) == ()


def test_async_provider_methods_are_rejected_at_spec_construction():
    class AsyncServices(RecordingServices):
        async def materialize_input(self, **kwargs):  # type: ignore[override]
            return super().materialize_input(**kwargs)

    with pytest.raises(
        TypeError,
        match=r"lifecycle methods must be synchronous; async methods: materialize_input",
    ):
        _spec(AsyncServices())


def test_hidden_provider_awaitable_is_rejected_without_coroutine_leak():
    from avalanche.execution_services import _run_with_execution_services

    class HiddenAsyncServices(RecordingServices):
        def materialize_input(self, **kwargs):  # type: ignore[override]
            async def materialize():
                return super().materialize_input(**kwargs)

            return materialize()

    task_spec = ava.ExecutionTaskSpec(
        run_id="run",
        workflow_name="flow",
        node_id="task_1",
        node_name="task",
        node_slug="task",
        executor_type="local",
    )
    with pytest.raises(TypeError, match="materialize_input returned an awaitable"):
        _run_with_execution_services(
            lambda payload: payload.scalar,
            _spec(HiddenAsyncServices()),
            task_spec,
            ManagedInput,
            _raw_input(),
            ("payload",),
            (),
            (),
            {},
            num_returns=1,
        )


def test_cleanup_failures_do_not_mask_primary_task_failure():
    from avalanche.execution_services import _run_with_execution_services

    class CleanupFailureServices(RecordingServices):
        def abort(self, *, session, error):
            raise RuntimeError("abort cleanup failed")

        def teardown(self, *, session):
            raise RuntimeError("teardown cleanup failed")

    def fail(payload: ManagedInput):
        raise ValueError("primary task failed")

    task_spec = ava.ExecutionTaskSpec(
        run_id="run",
        workflow_name="flow",
        node_id="fail_1",
        node_name="fail",
        node_slug="fail",
        executor_type="local",
    )
    with pytest.raises(ValueError, match="primary task failed") as error:
        _run_with_execution_services(
            fail,
            _spec(CleanupFailureServices()),
            task_spec,
            ManagedInput,
            _raw_input(),
            ("payload",),
            (),
            (),
            {},
            num_returns=1,
        )
    notes = getattr(error.value, "__notes__", ())
    assert any("abort cleanup failed" in note for note in notes)
    assert any("teardown cleanup failed" in note for note in notes)


def test_executor_owned_metadata_does_not_claim_user_keyword_names():
    task = ava.source(capture_kwargs)

    @ava.workflow(input=ManagedInput)
    def flow():
        return task(**ALL_INTERNAL_METADATA_KWARGS)

    handle = flow().run(
        executor=ava.LocalExecutor(),
        input=_raw_input(),
        execution_services=_spec(RecordingServices()),
    )
    assert handle.result(timeout=5) == ALL_INTERNAL_METADATA_KWARGS
    assert handle.execution_receipts(timeout=5)[0].value["node"] == "capture_kwargs_1"


@pytest.mark.ray
def test_real_ray_materializes_on_worker_and_returns_deterministic_hidden_receipts():
    ray = pytest.importorskip("ray")
    if ray.is_initialized():
        ray.shutdown()
    ray.init(
        num_cpus=2,
        ignore_reinit_error=True,
        include_dashboard=False,
    )
    try:
        driver_pid = os.getpid()

        class RayInput(ava.BaseInput):
            scalar: str
            values: list[str]
            worker_pid: int

        @dataclass(frozen=True)
        class RayRequest:
            run_key: str

        class RayServices:
            def probe(self, *, request, task):
                return request.run_key

            def negotiate(self, *, request, task, probe):
                return probe

            def open(self, *, request, task, negotiation, upstream_receipts):
                return task, tuple(upstream_receipts)

            def materialize_input(self, *, session, input_type, input):
                return {
                    "scalar": input["scalar"]["value"],
                    "values": [item["value"] for item in input["values"]],
                    "worker_pid": os.getpid(),
                }

            def finalize(self, *, session):
                task, _upstream_receipts = session
                return {"node": task.node_id, "pid": os.getpid()}

            def abort(self, *, session, error):
                return None

            def teardown(self, *, session):
                return None

        def left_fn(payload):
            assert payload.worker_pid != driver_pid
            return [payload.scalar, *payload.values]

        left_fn.__annotations__["payload"] = RayInput
        left = ava.source(left_fn)

        @ava.source
        def right(value: str):
            return value

        @ava.step
        def join(left_value, right_value):
            return {"left": left_value, "right": right_value, "pid": os.getpid()}

        @ava.workflow(input=RayInput)
        def flow():
            return join(left(), right(ava.input.scalar))

        spec = ava.ExecutionServicesSpec(
            service=RayServices(),
            request=RayRequest("ray-run"),
        )
        handle = flow().run(
            executor=ava.RayExecutor(),
            input={
                "scalar": {"value": "scalar"},
                "values": [
                    {"value": "first"},
                    {"value": "second"},
                    {"value": "first"},
                ],
            },
            execution_services=spec,
        )
        result = handle.result(timeout=30)
        receipts = handle.execution_receipts(timeout=30)

        assert result["left"] == ["scalar", "first", "second", "first"]
        assert result["right"] == "scalar"
        assert result["pid"] != driver_pid
        assert [(receipt.node_id, receipt.node_slug) for receipt in receipts] == [
            ("join_1", "join")
        ]
        assert receipts[0].value["node"] == "join_1"
        assert receipts[0].value["pid"] != driver_pid
    finally:
        ray.shutdown()


@pytest.mark.ray
def test_real_ray_executor_owned_metadata_is_strictly_positional_only():
    ray = pytest.importorskip("ray")
    if ray.is_initialized():
        ray.shutdown()
    ray.init(
        num_cpus=2,
        ignore_reinit_error=True,
        include_dashboard=False,
        runtime_env={
            "env_vars": {"PYTHONPATH": os.path.dirname(__file__)},
        },
    )
    try:
        task = ava.source(capture_kwargs)

        @ava.workflow
        def flow():
            return task(**ALL_INTERNAL_METADATA_KWARGS)

        handle = flow().run(
            executor=ava.RayExecutor(),
            execution_services=ava.ExecutionServicesSpec(
                service=MetadataCollisionRayServices(),
                request=ServiceRequest("ray-collision"),
            ),
        )

        assert handle.result(timeout=30) == ALL_INTERNAL_METADATA_KWARGS
        receipts = handle.execution_receipts(timeout=30)
        assert len(receipts) == 1
        assert receipts[0].node_id == "capture_kwargs_1"
        assert receipts[0].value == {"receipt-node": "capture_kwargs_1"}
    finally:
        ray.shutdown()


@pytest.mark.ray
def test_real_ray_malformed_multi_returns_abort_before_finalize_and_teardown_once():
    ray = pytest.importorskip("ray")
    if ray.is_initialized():
        ray.shutdown()
    ray.init(
        num_cpus=2,
        ignore_reinit_error=True,
        include_dashboard=False,
        runtime_env={
            "env_vars": {"PYTHONPATH": os.path.dirname(__file__)},
        },
    )
    try:
        event_recorder = ray.remote(num_cpus=0)(RayLifecycleEventRecorder)
        executor = ava.RayExecutor()
        task_spec = ava.ExecutionTaskSpec(
            run_id="run",
            workflow_name="flow",
            node_id="task_1",
            node_name="task",
            node_slug="task",
            executor_type="ray",
        )
        malformed_results = [(), ("only",), ("one", "two", "three")]
        for malformed_result in malformed_results:
            recorder = event_recorder.remote()
            payload_refs, _receipt_ref, status_ref = executor.submit_with_services(
                return_value,
                ava.ExecutionServicesSpec(
                    service=MalformedReturnRayServices(recorder),
                    request=ServiceRequest("ray-malformed-return"),
                ),
                task_spec,
                None,
                None,
                (),
                (),
                2,
                malformed_result,
            )

            assert len(payload_refs) == 2
            with pytest.raises(ray.exceptions.RayTaskError) as error:
                ray.get(status_ref)
            assert "expected to return 2 values" in str(error.value)
            assert ray.get(recorder.read.remote()) == [
                "probe",
                "negotiate",
                "open",
                "materialize",
                "abort",
                "teardown",
            ]
    finally:
        ray.shutdown()
