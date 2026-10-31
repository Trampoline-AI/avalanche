"""Bounded previews of standard step inputs and outputs, from worker to wire."""

from __future__ import annotations

import inspect
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from avalanche.runtime.providers import LoggerInstance
from runtime.operator import Operator
from runtime.operator.convert_v2 import node_snapshot_from_v2, node_snapshot_to_v2
from runtime.operator.models import NodeStatus, RunState, RunStatus
from runtime.operator.step_io import (
    StepIo,
    StepSignatures,
    StepValuePreview,
    StepValuePreviewKind,
    preview_step_inputs,
    preview_value,
    step_signatures,
)


class Ticket(BaseModel):
    title: str
    tags: list[str]


@dataclass
class Point:
    x: int
    y: int


class Opaque:
    def __repr__(self) -> str:
        return "Opaque(handle=7)"


class BrokenRepr:
    def __repr__(self) -> str:
        raise RuntimeError("no repr")


def decoded(preview: StepValuePreview) -> object:
    return json.loads(preview.preview_json)


@pytest.mark.parametrize(
    ("value", "expected", "type_name"),
    [
        (None, None, "NoneType"),
        (3, 3, "int"),
        ("hello", "hello", "str"),
        ({"a": [1, 2.5, True]}, {"a": [1, 2.5, True]}, "dict"),
        ((1, "two"), [1, "two"], "tuple"),
        (Ticket(title="t", tags=["x"]), {"title": "t", "tags": ["x"]}, f"{__name__}.Ticket"),
        (Point(x=1, y=2), {"x": 1, "y": 2}, f"{__name__}.Point"),
        (float("inf"), "inf", "float"),
        (Path("/tmp/a"), "/tmp/a", f"{type(Path()).__module__}.{type(Path()).__qualname__}"),
    ],
)
def test_plain_data_previews_as_complete_json(value, expected, type_name):
    preview = preview_value(value)

    assert preview.kind is StepValuePreviewKind.JSON
    assert preview.type_name == type_name
    assert decoded(preview) == expected
    assert preview.truncated is False


def test_opaque_values_preview_as_bounded_repr():
    preview = preview_value(Opaque())

    assert preview.kind is StepValuePreviewKind.REPR
    assert decoded(preview) == "Opaque(handle=7)"
    assert preview.truncated is False


def test_unbuildable_previews_are_marked_unavailable_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="runtime.operator.step_io"):
        preview = preview_value(BrokenRepr())

    assert preview.kind is StepValuePreviewKind.REPR
    assert preview.type_name == f"{__name__}.BrokenRepr"
    assert "RuntimeError" in decoded(preview)
    assert preview.truncated is True
    assert [record.exc_info[0] for record in caplog.records] == [RuntimeError]


@pytest.mark.parametrize(
    "value",
    [
        ["\x01" * 512 for _ in range(40)],
        {"\x01" * 300 + str(index): "\x02" * 512 for index in range(20)},
        Opaque.__new__(type("Noisy", (), {"__repr__": lambda self: "\x03" * 50_000})),
    ],
    ids=["escaped-strings", "escaped-keys", "escaped-repr"],
)
def test_escaped_characters_count_against_the_budget(value):
    preview = preview_value(value)

    assert preview.truncated is True
    assert len(preview.preview_json) <= 20_000


def test_integers_stay_exact_or_become_bounded_text():
    preview = preview_value([2**53 - 1, 2**60, 10**5000])

    exact, large, huge = decoded(preview)
    assert exact == 2**53 - 1
    assert large == str(2**60)
    assert isinstance(huge, str) and "…" in huge
    assert preview.truncated is True


def test_large_values_are_cut_with_explicit_markers():
    long_list = preview_value(list(range(10_000)))
    long_text = preview_value("x" * 10_000)
    deep = preview_value([[[[[[[["bottom"]]]]]]]])
    wide = preview_value({f"key{index}": index for index in range(1_000)})

    items = decoded(long_list)
    assert isinstance(items, list)
    assert items[:3] == [0, 1, 2]
    assert isinstance(items[-1], str) and "more items" in items[-1]
    assert len(items) < 100
    text = decoded(long_text)
    assert isinstance(text, str) and len(text) < 1_000 and text.startswith("xxx")
    assert "…" in json.dumps(decoded(deep), ensure_ascii=False)
    keys = decoded(wide)
    assert isinstance(keys, dict) and "…" in keys and len(keys) < 100
    for preview in (long_list, long_text, deep, wide):
        assert preview.truncated is True
        assert len(preview.preview_json) < 32_768


def test_huge_nested_values_stay_within_the_preview_budget():
    value = [{"text": "y" * 600, "rows": list(range(50))} for _ in range(500)]

    preview = preview_value(value)

    assert preview.truncated is True
    assert len(preview.preview_json) <= 20_000


def test_inputs_are_named_by_parameter_and_omit_injected_loggers():
    def step(first, second=2, *rest, logger=None, **options):
        return first

    logger = LoggerInstance(logging.getLogger("test"), {})
    inputs = preview_step_inputs(
        step_signatures(step), (1, 2, 3), {"logger": logger, "flag": True}
    )

    assert [(item.name, decoded(item.value)) for item in inputs.parameters] == [
        ("first", 1),
        ("second", 2),
        ("rest", [3]),
        ("options", {"flag": True}),
    ]


def test_inputs_that_do_not_bind_raise_like_the_call():
    def step(only):
        return only

    with pytest.raises(TypeError):
        preview_step_inputs(step_signatures(step), (1, 2), {})


def keyword_signature(names: list[str]) -> StepSignatures:
    signature = inspect.Signature(
        [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY) for name in names]
    )
    return StepSignatures(declared=signature, own=signature)


def test_long_argument_names_and_lists_are_capped_with_markers():
    long_name = "n" * 1_000
    names = [f"p{index}" for index in range(300)]

    [long_input] = preview_step_inputs(
        keyword_signature([long_name]), (), {long_name: 1}
    ).parameters
    capped = preview_step_inputs(
        keyword_signature(names), (), {name: 1 for name in names}
    ).parameters

    assert len(long_input.name) == 256 and long_input.name.endswith("…")
    assert len(capped) == 256
    assert [item.name for item in capped[:2]] == ["p0", "p1"]
    assert capped[-1].name == "…"
    assert capped[-1].value.truncated is True
    assert "45 more" in decoded(capped[-1].value)


def test_step_io_round_trips_through_the_v2_node_snapshot():
    from runtime.operator.models import NodeSnapshot

    step_io = StepIo(
        inputs=preview_step_inputs(
            step_signatures(lambda value: value), (Opaque(),), {}
        ).parameters,
        output=preview_value({"ok": True}),
    )
    node = NodeSnapshot(node_id="n", name="n", node_type="step", step_io=step_io)

    message = node_snapshot_to_v2(node)

    assert node_snapshot_from_v2(message).step_io == step_io
    assert not node_snapshot_to_v2(NodeSnapshot("m", "m", "step")).HasField("step_io")


WORKFLOW = """
import avalanche as ava
from avalanche.runtime.providers import Logger


@ava.source
def load():
    return {"rows": list(range(500)), "label": "batch"}


@ava.step
def summarize(batch: dict, logger=Logger()) -> dict:
    logger.info("summarizing")
    return {"count": len(batch["rows"]), "label": batch["label"]}


@ava.step
def explode(summary: dict) -> None:
    raise ValueError("boom")


@ava.workflow
def flow():
    return explode(summarize(load()))
"""


def wait_terminal(operator: Operator, run_id: str, timeout: float = 30.0) -> RunState:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = operator.get_run(run_id)
        if run is not None and run.status in {
            RunStatus.SUCCESS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return run
        time.sleep(0.03)
    raise AssertionError(f"run {run_id} did not finish")


def node_named(run: RunState, name: str):
    [node] = [node for node in run.nodes.values() if node.name == name]
    return node


def assert_captured_step_values(run: RunState) -> None:
    source = node_named(run, "load")
    assert source.step_io is not None
    assert source.step_io.inputs == ()
    assert source.step_io.output is not None
    assert source.step_io.output.truncated is True

    summarize = node_named(run, "summarize")
    assert summarize.status is NodeStatus.SUCCESS
    assert summarize.step_io is not None
    [batch] = summarize.step_io.inputs
    assert batch.name == "batch"
    assert batch.value.truncated is True
    batch_preview = json.loads(batch.value.preview_json)
    assert batch_preview["label"] == "batch"
    assert batch_preview["rows"][:2] == [0, 1]
    assert summarize.step_io.output is not None
    assert json.loads(summarize.step_io.output.preview_json) == {"count": 500, "label": "batch"}

    failed = node_named(run, "explode")
    assert failed.status is NodeStatus.FAILED
    assert failed.step_io is not None
    assert [item.name for item in failed.step_io.inputs] == ["summary"]
    assert failed.step_io.output is None


def test_spawned_local_run_retains_step_inputs_and_outputs(tmp_path):
    workflow = tmp_path / "step_io_flow.py"
    workflow.write_text(WORKFLOW)
    operator = Operator([str(workflow)], executor_backend="local", watch=False, schedule=False)
    try:
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status is RunStatus.FAILED
        assert_captured_step_values(run)
        summaries = operator.list_run_summaries()
        snapshot = operator.get_run_snapshot(
            run.run_id,
            operator_instance_id=summaries.operator_instance_id,
            as_of_sequence=summaries.as_of_sequence,
        )
        assert snapshot is not None
        assert {node.name: node.step_io for node in snapshot.nodes} == {
            node.name: node.step_io for node in run.nodes.values()
        }
    finally:
        operator.close()


@pytest.mark.ray
def test_ray_run_previews_step_values_inside_the_task(tmp_path):
    pytest.importorskip("ray")
    workflow = tmp_path / "step_io_flow.py"
    workflow.write_text(WORKFLOW)
    operator = Operator(
        [str(workflow)],
        executor_backend="ray",
        ray_init_kwargs={"address": "local", "num_cpus": 2, "include_dashboard": False},
        prepare_timeout=30.0,
        watch=False,
        schedule=False,
    )
    try:
        run = wait_terminal(operator, operator.start_run("flow"), timeout=90)
        assert run.status is RunStatus.FAILED
        assert_captured_step_values(run)
    finally:
        operator.close()


class RecordingQueue:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def put(self, event: dict[str, object]) -> None:
        self.events.append(event)


def test_sync_and_async_steps_publish_inputs_then_output():
    import asyncio

    from runtime.operator.run_worker import _with_step_io

    def add(left: int, right: int) -> int:
        return left + right

    async def add_later(left: int, right: int) -> int:
        return left + right

    for fn in (add, add_later):
        queue = RecordingQueue()
        wrapped = _with_step_io("add", fn, queue)
        result = wrapped(1, right=2)
        if inspect.iscoroutine(result):
            result = asyncio.run(result)

        assert result == 3
        assert [event["type"] for event in queue.events] == ["step_inputs", "step_output"]
        inputs = json.loads(str(queue.events[0]["inputs"]))["parameters"]
        assert [(item["name"], item["value"]["preview_json"]) for item in inputs] == [
            ("left", "1"),
            ("right", "2"),
        ]
        assert json.loads(str(queue.events[1]["output"]))["preview_json"] == "3"


def test_failing_step_publishes_only_inputs_and_keeps_its_error():
    from runtime.operator.run_worker import _with_step_io

    def explode(value: int) -> int:
        raise ValueError("boom")

    queue = RecordingQueue()
    with pytest.raises(ValueError, match="boom"):
        _with_step_io("explode", explode, queue)(1)

    assert [event["type"] for event in queue.events] == ["step_inputs"]


@pytest.mark.parametrize("marker", ["__agent_step__", "__classifier_step__"])
def test_agent_and_classifier_steps_are_left_to_their_own_evidence(marker):
    from runtime.operator.run_worker import _with_step_io

    def step(value: int) -> int:
        return value

    setattr(step, marker, object())
    queue = RecordingQueue()

    assert _with_step_io("step", step, queue) is step


def protocol_operator(status: RunStatus = RunStatus.RUNNING):
    import threading
    from types import SimpleNamespace

    from runtime.operator.models import NodeState

    operator = Operator([], watch=False, schedule=False)
    run = RunState(run_id="run-io", flow_name="flow", status=status)
    run.nodes["step_1"] = NodeState("step_1", "step", "step", status=NodeStatus.RUNNING)
    operator._runs[run.run_id] = run
    handle = SimpleNamespace(
        cancel_event=threading.Event(), result_bundle=None, success_quiesced=False
    )

    def apply(event_type: str, **payload: object) -> None:
        operator._apply_event(
            run.run_id, handle, {"type": event_type, "node_id": "step_1", **payload}
        )

    return operator, run, apply


def inputs_json(value: object) -> str:
    def step(value):
        return value

    return preview_step_inputs(step_signatures(step), (value,), {}).model_dump_json()


def test_a_retry_replaces_inputs_and_clears_the_previous_output():
    operator, run, apply = protocol_operator()
    try:
        apply("step_inputs", inputs=inputs_json(1))
        apply("step_output", output=preview_value(10).model_dump_json())
        apply("step_inputs", inputs=inputs_json(2))

        step_io = run.nodes["step_1"].step_io
        assert step_io is not None
        assert decoded(step_io.inputs[0].value) == 2
        assert step_io.output is None
    finally:
        operator.close()


@pytest.mark.parametrize(
    "event",
    [
        ("step_output", {"output": preview_value(1).model_dump_json()}),
        ("step_inputs", {"inputs": '{"parameters": "not a list"}'}),
        ("step_inputs", {"inputs": {"parameters": []}}),
        ("step_inputs", {"inputs": inputs_json(1), "extra": True}),
    ],
    ids=["output-before-inputs", "invalid-payload", "payload-not-json-text", "extra-key"],
)
def test_malformed_step_value_events_are_protocol_errors(event):
    from runtime.operator.operator import _CoordinatorProtocolError

    operator, run, apply = protocol_operator()
    event_type, payload = event
    try:
        with pytest.raises(_CoordinatorProtocolError):
            apply(event_type, **payload)
        assert run.nodes["step_1"].step_io is None
    finally:
        operator.close()


def test_cancelled_runs_ignore_late_step_values():
    operator, run, apply = protocol_operator(RunStatus.CANCELLED)
    try:
        apply("step_inputs", inputs=inputs_json(1))
        assert run.nodes["step_1"].step_io is None
    finally:
        operator.close()


def test_decorated_steps_are_named_by_their_declared_parameters():
    import functools

    from runtime.operator.run_worker import _with_step_io

    def retrying(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        return wrapper

    @retrying
    def scale(value: int, factor: int = 2, logger=None) -> int:
        return value * factor

    queue = RecordingQueue()
    logger = LoggerInstance(logging.getLogger("test"), {})

    assert _with_step_io("scale", scale, queue)(3, logger=logger) == 6
    inputs = json.loads(str(queue.events[0]["inputs"]))["parameters"]
    assert [item["name"] for item in inputs] == ["value"]


def test_decorators_accepting_more_than_the_wrapped_function_bind_their_own_call():
    import functools

    def injecting(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            kwargs.pop("trace_id")
            return fn(*args, **kwargs)

        return wrapper

    @injecting
    def step(value: int) -> int:
        return value

    inputs = preview_step_inputs(step_signatures(step), (1,), {"trace_id": "t-1"})

    assert [item.name for item in inputs.parameters] == ["args", "kwargs"]
    with pytest.raises(TypeError):
        preview_step_inputs(step_signatures(lambda value: value), (1, 2), {})


def test_step_io_imports_without_loading_the_operator() -> None:
    """Hosts that only build previews (a remote step worker) do not pay for the Operator."""
    import subprocess
    import sys

    probe = (
        "import sys, runtime.operator.step_io; "
        "print(sorted(m for m in ('runtime.operator.operator', 'runtime.operator.server', "
        "'grpc', 'multiprocessing') if m in sys.modules))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", probe], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert loaded == "[]"
