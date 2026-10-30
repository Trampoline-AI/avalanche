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
    StepValuePreview,
    StepValuePreviewKind,
    preview_step_inputs,
    preview_value,
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Opaque(), "Opaque(handle=7)"),
        (BrokenRepr(), f"<{__name__}.BrokenRepr: repr failed with RuntimeError>"),
    ],
)
def test_opaque_values_preview_as_bounded_repr(value, expected):
    preview = preview_value(value)

    assert preview.kind is StepValuePreviewKind.REPR
    assert decoded(preview) == expected
    assert preview.truncated is False


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
        inspect.signature(step), (1, 2, 3), {"logger": logger, "flag": True}
    )

    assert [(item.name, decoded(item.value)) for item in inputs.parameters] == [
        ("first", 1),
        ("second", 2),
        ("rest", [3]),
        ("options", {"flag": True}),
    ]


def test_inputs_that_do_not_bind_are_still_shown_positionally():
    def step(only):
        return only

    inputs = preview_step_inputs(inspect.signature(step), (1, 2), {"extra": 3})

    assert [item.name for item in inputs.parameters] == ["args[0]", "args[1]", "extra"]


def test_step_io_round_trips_through_the_v2_node_snapshot():
    from runtime.operator.models import NodeSnapshot

    step_io = StepIo(
        inputs=preview_step_inputs(
            inspect.signature(lambda value: value), (Opaque(),), {}
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
