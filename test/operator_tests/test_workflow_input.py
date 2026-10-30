"""Operator projections describe the declared run input and the values each run used."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import avalanche as ava
from runtime.operator import Operator
from runtime.operator.convert_v2 import (
    run_snapshot_from_v2,
    run_snapshot_to_v2,
    workflow_info_from_v2,
    workflow_info_to_v2,
)
from runtime.operator.models import RunInput, RunSnapshot, RunStatus, RunSummary
from runtime.operator.operator import _CoordinatorProtocolError, _validate_preparation_event
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.run_worker import _workflow_metadata
from runtime.operator.workflow_metadata import (
    MAX_PREPARED_INPUT_LENGTH,
    PreparedRunInput,
    WorkflowInputDeclaration,
)


def _write_workflows(root: Path) -> Path:
    path = root / "input_workflows.py"
    path.write_text(
        # Postponed annotations must still identify the steps that receive the input.
        "from __future__ import annotations\n"
        "import avalanche as ava\n"
        "from pydantic import Field\n"
        "class ReviewInput(ava.BaseInput):\n"
        "    repository: str = Field('acme/app', description='Repository to review')\n"
        "    owner: str = Field('acme', alias='repositoryOwner')\n"
        "    post_reviews: bool = False\n"
        "    limit: int = 3\n"
        # Excluded from serialization, so runs never record its value.
        "    token: str = Field('secret', exclude=True)\n"
        "class AuditInput(ava.BaseInput):\n"
        "    strict: bool = False\n"
        "@ava.source\n"
        "def load(config: ReviewInput) -> list[int]:\n"
        "    return list(range(config.limit))\n"
        # A different input model is never injected, so this step receives nothing.
        "@ava.step\n"
        "def audit(values: list[int], audit: AuditInput = AuditInput()) -> list[int]:\n"
        "    return values\n"
        "@ava.step\n"
        "def trim(values: list[int], limit: int) -> list[int]:\n"
        "    return values[:limit]\n"
        "@ava.dest\n"
        "def save(values: list[int]) -> None:\n"
        "    print(values)\n"
        "@ava.workflow(input=ReviewInput)\n"
        "def reviewed():\n"
        "    return save(trim(audit(load()), limit=ava.input.limit))\n"
        "class OpaqueInput(ava.BaseInput):\n"
        "    handle: object = Field(default_factory=object)\n"
        "@ava.source\n"
        "def opaque_source(config: OpaqueInput) -> int:\n"
        "    return 1\n"
        "@ava.workflow(input=OpaqueInput)\n"
        "def opaque():\n"
        "    return opaque_source()\n"
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


def _finished_snapshot(operator: Operator, run_id: str):
    deadline = time.monotonic() + 10
    while operator.get_run(run_id).status not in {RunStatus.SUCCESS, RunStatus.FAILED}:
        assert time.monotonic() < deadline
        time.sleep(0.03)
    return operator.get_latest_run_snapshot(
        run_id, operator_instance_id=operator.operator_instance_id
    )


def test_catalog_declares_input_model_and_receiving_steps(operator):
    info = _workflow(operator, "reviewed")
    declaration = WorkflowInputDeclaration.model_validate_json(info.input_declaration_json)

    assert declaration.type_name == "ReviewInput"
    assert {node_id.rsplit("_", 1)[0] for node_id in declaration.consumer_node_ids} == {
        "load",
        "trim",
    }
    properties = declaration.json_schema["properties"]
    assert isinstance(properties, dict)
    # Field names key the schema, the run values and the supplied fields alike.
    assert list(properties) == ["repository", "owner", "post_reviews", "limit", "token"]
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
    snapshot = _finished_snapshot(
        operator,
        operator.start_run(
            _workflow(operator, "reviewed").workflow_id,
            input={"limit": 2, "repositoryOwner": "octo"},
        ),
    )
    assert snapshot.summary.status == RunStatus.SUCCESS
    assert snapshot.input is not None
    assert json.loads(snapshot.input.values_json) == {
        "repository": "acme/app",
        "owner": "octo",
        "post_reviews": False,
        "limit": 2,
    }
    assert snapshot.input.supplied_fields == ("limit", "owner")
    declaration = WorkflowInputDeclaration.model_validate_json(
        snapshot.topology.input_declaration_json
    )
    assert declaration.type_name == "ReviewInput"


def test_run_without_declared_input_has_no_input_snapshot(operator):
    snapshot = _finished_snapshot(
        operator, operator.start_run(_workflow(operator, "plain").workflow_id)
    )
    assert snapshot.input is None


def test_run_without_caller_input_reports_every_default(operator):
    """Scheduled runs supply no input, so every recorded value is its declared default."""
    snapshot = _finished_snapshot(
        operator, operator.start_run(_workflow(operator, "reviewed").workflow_id)
    )

    assert snapshot.summary.status == RunStatus.SUCCESS
    assert json.loads(snapshot.input.values_json) == {
        "repository": "acme/app",
        "owner": "acme",
        "post_reviews": False,
        "limit": 3,
    }
    assert snapshot.input.supplied_fields == ()


def test_input_that_cannot_be_serialized_still_runs(operator):
    snapshot = _finished_snapshot(
        operator, operator.start_run(_workflow(operator, "opaque").workflow_id)
    )

    assert snapshot.summary.status == RunStatus.SUCCESS
    assert snapshot.input is None


def test_input_too_large_to_report_still_runs(operator):
    snapshot = _finished_snapshot(
        operator,
        operator.start_run(
            _workflow(operator, "reviewed").workflow_id,
            input={"repository": "r" * (MAX_PREPARED_INPUT_LENGTH + 1)},
        ),
    )
    assert snapshot.summary.status == RunStatus.SUCCESS
    assert snapshot.input is None
    assert snapshot.topology.input_declaration_json


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


class _Settings(ava.BaseInput):
    limit: int = 3


@ava.source
def _limited(settings: _Settings) -> int:
    return settings.limit


def _prepared_event() -> dict[str, object]:
    @ava.workflow(input=_Settings)
    def limited():
        return _limited()

    return {
        "type": "prepared",
        **_workflow_metadata(limited()),
        "input": PreparedRunInput(values_json='{"limit":3}', supplied_fields=[]).model_dump(),
    }


@pytest.mark.parametrize("values", ["reported", "too-large-to-report"])
def test_coordinator_accepts_a_consistent_prepared_input(values):
    event = _prepared_event()
    if values == "too-large-to-report":
        event["input"] = None
    assert _validate_preparation_event(event) == "prepared"


@pytest.mark.parametrize(
    "failure", ["undeclared-values", "unknown-consumer", "malformed-values", "oversized-values"]
)
def test_coordinator_rejects_inconsistent_prepared_input(failure):
    event = _prepared_event()
    if failure == "oversized-values":
        event["input"] = {
            "values_json": json.dumps({"limit": "x" * MAX_PREPARED_INPUT_LENGTH}),
            "supplied_fields": ["limit"],
        }
    elif failure == "undeclared-values":
        event["input_declaration_json"] = ""
    elif failure == "unknown-consumer":
        declaration = json.loads(event["input_declaration_json"])
        declaration["consumer_node_ids"] = ["absent"]
        event["input_declaration_json"] = json.dumps(declaration)
    else:
        event["input"] = {"values_json": '{"limit":3}', "supplied_fields": "limit"}

    with pytest.raises(_CoordinatorProtocolError):
        _validate_preparation_event(event)


def test_run_snapshot_input_survives_the_grpc_boundary():
    snapshot = RunSnapshot(
        operator_instance_id="operator-1",
        as_of_sequence=1,
        summary=RunSummary(run_id="run-1", flow_name="reviewed"),
        input=RunInput(values_json='{"limit":2}', supplied_fields=("limit", "owner")),
    )
    message = run_snapshot_to_v2(
        snapshot,
        cursor=pb.LifecycleCursorV2(),
        scope_ref=pb.ScopeReferenceV2(reference="operator-1"),
        trace_detail_ref_for=lambda *_: pb.ActivityDetailRefV2(),
        activity_continuations={},
        log_continuation=None,
    )

    assert run_snapshot_from_v2(message).input == snapshot.input
    without_input = run_snapshot_to_v2(
        RunSnapshot(
            operator_instance_id="operator-1",
            as_of_sequence=1,
            summary=snapshot.summary,
        ),
        cursor=pb.LifecycleCursorV2(),
        scope_ref=pb.ScopeReferenceV2(reference="operator-1"),
        trace_detail_ref_for=lambda *_: pb.ActivityDetailRefV2(),
        activity_continuations={},
        log_continuation=None,
    )
    assert run_snapshot_from_v2(without_input).input is None
