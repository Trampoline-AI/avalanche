"""Operator projections describe the declared run input and the values each run used."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from runtime.operator import Operator
from runtime.operator.convert_v2 import workflow_info_from_v2, workflow_info_to_v2
from runtime.operator.models import RunStatus
from runtime.operator.workflow_metadata import WorkflowInputDeclaration


def _write_workflows(root: Path) -> Path:
    path = root / "input_workflows.py"
    path.write_text(
        "import avalanche as ava\n"
        "from pydantic import Field\n"
        "class ReviewInput(ava.BaseInput):\n"
        "    repository: str = Field('acme/app', description='Repository to review')\n"
        "    post_reviews: bool = False\n"
        "    limit: int = 3\n"
        "@ava.source\n"
        "def load(config: ReviewInput) -> list[int]:\n"
        "    return list(range(config.limit))\n"
        "@ava.step\n"
        "def trim(values: list[int], limit: int) -> list[int]:\n"
        "    return values[:limit]\n"
        "@ava.dest\n"
        "def save(values: list[int]) -> None:\n"
        "    print(values)\n"
        "@ava.workflow(input=ReviewInput)\n"
        "def reviewed():\n"
        "    return save(trim(load(), limit=ava.input.limit))\n"
        "@ava.source\n"
        "def constant() -> int:\n"
        "    return 1\n"
        "@ava.workflow\n"
        "def plain():\n"
        "    return constant()\n"
    )
    return path


@pytest.fixture
def operator(tmp_path):
    instance = Operator([str(_write_workflows(tmp_path))], watch=False, schedule=False)
    try:
        yield instance
    finally:
        instance.close()


def _workflow(operator: Operator, name: str):
    return next(item for item in operator.list_workflows() if item.name == name)


def test_catalog_declares_input_model_and_receiving_steps(operator):
    info = _workflow(operator, "reviewed")
    declaration = WorkflowInputDeclaration.model_validate_json(info.input_declaration_json)

    assert declaration.type_name == "ReviewInput"
    assert declaration.consumer_node_ids == [
        node_id for node_id in info.node_ids if not node_id.startswith("save")
    ]
    properties = declaration.json_schema["properties"]
    assert isinstance(properties, dict)
    assert properties["repository"] == {
        "default": "acme/app",
        "description": "Repository to review",
        "title": "Repository",
        "type": "string",
    }
    assert workflow_info_from_v2(workflow_info_to_v2(info)).input_declaration_json == (
        info.input_declaration_json
    )


def test_workflow_without_input_declares_none(operator):
    assert _workflow(operator, "plain").input_declaration_json == ""


def test_run_snapshot_reports_validated_values_and_supplied_fields(operator):
    run_id = operator.start_run(_workflow(operator, "reviewed").workflow_id, input={"limit": 2})
    deadline = time.monotonic() + 10
    while operator.get_run(run_id).status not in {RunStatus.SUCCESS, RunStatus.FAILED}:
        assert time.monotonic() < deadline
        time.sleep(0.03)

    snapshot = operator.get_latest_run_snapshot(
        run_id, operator_instance_id=operator.operator_instance_id
    )
    assert snapshot.summary.status == RunStatus.SUCCESS
    assert snapshot.input is not None
    assert json.loads(snapshot.input.values_json) == {
        "repository": "acme/app",
        "post_reviews": False,
        "limit": 2,
    }
    assert snapshot.input.supplied_fields == ("limit",)
    declaration = WorkflowInputDeclaration.model_validate_json(
        snapshot.topology.input_declaration_json
    )
    assert declaration.type_name == "ReviewInput"


def test_run_without_declared_input_has_no_input_snapshot(operator):
    run_id = operator.start_run(_workflow(operator, "plain").workflow_id)
    deadline = time.monotonic() + 10
    while operator.get_run(run_id).status not in {RunStatus.SUCCESS, RunStatus.FAILED}:
        assert time.monotonic() < deadline
        time.sleep(0.03)

    snapshot = operator.get_latest_run_snapshot(
        run_id, operator_instance_id=operator.operator_instance_id
    )
    assert snapshot.input is None


def test_invalid_input_fails_the_run_during_preparation(operator):
    run_id = operator.start_run(
        _workflow(operator, "reviewed").workflow_id, input={"limit": "many"}
    )
    deadline = time.monotonic() + 10
    while operator.get_run(run_id).status is not RunStatus.FAILED:
        assert operator.get_run(run_id).status is not RunStatus.SUCCESS
        assert time.monotonic() < deadline
        time.sleep(0.03)

    run = operator.get_run(run_id)
    assert run.nodes == {}
    assert run.input is None
