"""Discovery and historical declarations remain usable without executing classifiers."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from avalanche.classifier.models import ClassifierDeclaration
from avalanche.step_interface import StepInterface
from runtime.operator import Operator, WorkflowRegistry
from runtime.operator.convert_v2 import (
    workflow_info_from_v2,
    workflow_info_to_v2,
    workflow_topology_from_v2,
    workflow_topology_to_v2,
)
from runtime.operator.models import RunState, RunStatus
from runtime.operator.proto import operator_pb2 as pb

from .test_classifier_execution import (
    QUESTIONS,
    classifier_node,
    read_invocations,
    wait_terminal,
)
from .test_classifier_execution import (
    typesafe_service as typesafe_service,
)


def _write_discovery_workflow(root: Path) -> Path:
    workflow = root / "discover_classifier.py"
    workflow.write_text(
        "from __future__ import annotations\n"
        "import os\n"
        "import socket\n"
        "import avalanche as ava\n"
        "from pydantic import BaseModel, Field\n"
        "if os.environ.get('CLASSIFIER_TEST_FORBID_IMPORT'):\n"
        "    raise RuntimeError('cached discovery must not reimport this module')\n"
        "def forbidden_connection(*args, **kwargs):\n"
        "    raise AssertionError('discovery attempted a network connection')\n"
        "socket.socket.connect = forbidden_connection\n"
        "def forbidden_input():\n"
        "    raise AssertionError('discovery instantiated input values')\n"
        "class Message(BaseModel):\n"
        "    content: str = Field(min_length=1, description='Message body')\n"
        "class CallInput(BaseModel):\n"
        "    item: Message\n"
        "    history: list[str] = Field(default_factory=forbidden_input)\n"
        f"@ava.classifier_step(questions={QUESTIONS!r}, timeout=3.0, input_model=CallInput)\n"
        "async def classify(messages: list[Message], *, classifier: ava.Classifier)"
        " -> list[Message]:\n"
        "    raise AssertionError('discovery executed the classifier body')\n"
        "@ava.source\n"
        "def load() -> list[Message]:\n"
        "    raise AssertionError('discovery executed the source body')\n"
        "@ava.step\n"
        "def prepare(messages: list[Message], limit: int = 3) -> list[Message]:\n"
        "    raise AssertionError('discovery executed the step body')\n"
        "@ava.agent_step(ava.Signature('query: str -> reply: str'))\n"
        "async def draft(messages: list[Message], *, agent: ava.Agent) -> list[Message]:\n"
        "    raise AssertionError('discovery executed the agent body')\n"
        "@ava.dest\n"
        "def save(messages: list[Message]) -> None:\n"
        "    raise AssertionError('discovery executed the destination body')\n"
        "@ava.workflow(classifier_defaults={'model': 'discovery-model', 'timeout': 6.0})\n"
        "def discoverable():\n"
        "    return save(draft(classify(prepare(load()))))\n"
    )
    return workflow


def _assert_structured_declaration(declaration: ClassifierDeclaration) -> None:
    assert list(declaration.questions) == ["urgent", "severity", "department"]
    assert declaration.questions["urgent"].instructions == {
        "ask": ["Does this require action today?", {"business_hours": True}]
    }
    severity = declaration.questions["severity"]
    assert severity.type == "score"
    assert severity.instructions == ["Assess severity", {"consider": "customer impact"}]
    assert severity.criteria == ["minor", {"impact": ["service unavailable"]}]
    department = declaration.questions["department"]
    assert department.type == "choice"
    assert list(department.criteria) == ["billing", "technical"]
    assert department.criteria["billing"] == {"examples": ["duplicate charge"]}
    assert department.criteria["technical"] is None


def test_discovery_and_cache_preserve_ordered_structured_questions_without_key_or_network(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("CLASSIFIER_TEST_FORBID_IMPORT", raising=False)
    workflow = _write_discovery_workflow(tmp_path)
    cache_dir = tmp_path / "cache"
    registry = WorkflowRegistry(cache_dir=cache_dir)
    registry.scan([str(workflow)])
    assert registry.list_diagnostics() == []
    [descriptor] = registry.descriptors()
    [(node_id, metadata_json)] = descriptor.classifier_metadata_json
    declaration = ClassifierDeclaration.model_validate_json(metadata_json)
    _assert_structured_declaration(declaration)
    assert declaration.runtime.model == "discovery-model"
    assert declaration.runtime.timeout == 3.0
    [step_input] = declaration.step_inputs
    assert step_input.name == "messages"
    assert step_input.json_schema["items"] == {"$ref": "#/$defs/Message"}
    assert declaration.input_schema["properties"]["item"] == {"$ref": "#/$defs/Message"}
    assert declaration.step_output.json_schema["items"] == {"$ref": "#/$defs/Message"}
    interfaces = {
        dict(descriptor.display_names)[key]: StepInterface.model_validate_json(value)
        for key, value in descriptor.step_interface_json
    }
    assert set(interfaces) == {"load", "prepare", "classify", "draft", "save"}
    assert interfaces["load"].step_inputs == []
    assert interfaces["load"].step_output == declaration.step_output
    assert [(item.name, item.required) for item in interfaces["prepare"].step_inputs] == [
        ("messages", True),
        ("limit", False),
    ]
    assert interfaces["draft"].step_inputs == declaration.step_inputs
    assert interfaces["draft"].step_output == declaration.step_output
    assert interfaces["save"].step_output.json_schema == {"type": "null"}

    # A restart must serve the validated cache without importing user code again.
    monkeypatch.setenv("CLASSIFIER_TEST_FORBID_IMPORT", "1")
    restarted = WorkflowRegistry(cache_dir=cache_dir)
    restarted.scan([str(workflow)])
    assert restarted.list_diagnostics() == []
    [catalog_workflow] = restarted.list_workflows()
    assert catalog_workflow.node_types[node_id] == "step"
    cached = ClassifierDeclaration.model_validate_json(
        catalog_workflow.classifier_metadata_json[node_id]
    )
    _assert_structured_declaration(cached)
    assert cached.runtime.model == "discovery-model"
    assert cached.runtime.timeout == 3.0
    assert cached.step_inputs == declaration.step_inputs
    assert cached.step_output == declaration.step_output
    assert cached.input_schema == declaration.input_schema
    current = workflow_info_from_v2(
        pb.FlowInfoV2.FromString(workflow_info_to_v2(catalog_workflow).SerializeToString())
    )
    assert {
        current.display_names[key]: StepInterface.model_validate_json(value)
        for key, value in current.step_interface_json.items()
    } == interfaces
    [agent_json] = current.agent_metadata_json.values()
    assert [field["name"] for field in json.loads(agent_json)["signature"]["inputs"]] == [
        "query"
    ]


def _run_declaration(run: RunState) -> ClassifierDeclaration:
    return ClassifierDeclaration.model_validate_json(
        dict(run.topology.classifier_metadata_json)[classifier_node(run)]
    )


def _revised_questions():
    questions = copy.deepcopy(QUESTIONS)
    questions["urgent"]["instructions"] = {"ask": ["Is this a production outage?"]}
    questions["severity"]["criteria"] = ["cosmetic", {"impact": ["all customers blocked"]}]
    # Reordering author input must not reorder previously retained declarations.
    return {name: questions[name] for name in ("department", "urgent", "severity")}


def _write_schema_workflow(root: Path, *, revised: bool = False) -> Path:
    workflow = root / "classifier_flow.py"
    annotation = "list[str]" if revised else "str"
    field = "items" if revised else "item"
    source = "['operator-private-state']" if revised else "'operator-private-state'"
    output = "str" if revised else "ava.ClassificationResult"
    result = "result.model" if revised else "result"
    questions = _revised_questions() if revised else QUESTIONS
    workflow.write_text(
        "from __future__ import annotations\n"
        "import avalanche as ava\n"
        "from pydantic import BaseModel\n"
        "class CallInput(BaseModel):\n"
        f"    {field}: {annotation}\n"
        "@ava.source\n"
        f"def load() -> {annotation}:\n"
        f"    return {source}\n"
        "@ava.step\n"
        f"def prepare(ticket: {annotation}) -> {annotation}:\n"
        "    return ticket\n"
        f"@ava.classifier_step(questions={questions!r}, input_model=CallInput)\n"
        f"async def classify(ticket: {annotation}, *, classifier: ava.Classifier)"
        f" -> {output}:\n"
        f"    result = await classifier(state={{{field!r}: ticket}})\n"
        f"    return {result}\n"
        "@ava.agent_step(ava.Signature('query: str -> reply: str'))\n"
        f"async def review(result: {output}, *, agent: ava.Agent) -> {output}:\n"
        "    return result\n"
        "@ava.dest\n"
        f"def save(result: {output}) -> {output}:\n"
        "    return result\n"
        "@ava.workflow(classifier_defaults={"
        "'model': 'operator-request-model', 'timeout': 10.0})\n"
        "def flow():\n"
        "    return save(review(classify(prepare(load()))))\n"
    )
    return workflow


def _assert_original_schema(declaration: ClassifierDeclaration) -> None:
    [step_input] = declaration.step_inputs
    assert step_input.name == "ticket"
    assert step_input.json_schema == {"type": "string"}
    assert set(declaration.input_schema["properties"]) == {"item"}
    assert declaration.input_schema["properties"]["item"]["type"] == "string"
    assert declaration.step_output.json_schema["type"] == "object"
    assert "answers" in declaration.step_output.json_schema["properties"]


def _assert_revised_schema(declaration: ClassifierDeclaration) -> None:
    [step_input] = declaration.step_inputs
    assert step_input.json_schema == {"type": "array", "items": {"type": "string"}}
    assert set(declaration.input_schema["properties"]) == {"items"}
    assert declaration.input_schema["properties"]["items"]["type"] == "array"
    assert declaration.step_output.json_schema == {"type": "string"}


@pytest.mark.usefixtures("typesafe_service")
def test_historical_questions_and_answers_survive_reload_and_source_removal(tmp_path):
    workflow = _write_schema_workflow(tmp_path)
    operator = Operator([str(workflow)], executor_backend="local", watch=False, schedule=False)
    try:
        original = wait_terminal(operator, operator.start_run("flow"))
        assert original.status == RunStatus.SUCCESS
        _assert_structured_declaration(_run_declaration(original))
        _assert_original_schema(_run_declaration(original))
        original_interfaces = dict(original.topology.step_interface_json)
        assert set(original_interfaces) == set(original.topology.node_ids)
        assert set(dict(original.topology.display_names).values()) == {
            "load",
            "prepare",
            "classify",
            "review",
            "save",
        }

        _write_schema_workflow(tmp_path, revised=True)
        operator._refresh_workflows()
        [current] = operator.list_workflows()
        current_declaration = ClassifierDeclaration.model_validate_json(
            current.classifier_metadata_json[classifier_node(original)]
        )
        assert list(current_declaration.questions) == ["department", "urgent", "severity"]
        _assert_revised_schema(current_declaration)
        assert current_declaration.questions["urgent"].instructions == {
            "ask": ["Is this a production outage?"]
        }
        current_wire = workflow_info_from_v2(
            pb.FlowInfoV2.FromString(workflow_info_to_v2(current).SerializeToString())
        )
        assert set(current_wire.step_interface_json) == set(original_interfaces)
        assert all(
            current_wire.step_interface_json[node_id] != interface
            for node_id, interface in original_interfaces.items()
        )
        revised = wait_terminal(operator, operator.start_run("flow"))
        assert revised.status == RunStatus.SUCCESS
        assert list(_run_declaration(revised).questions) == ["department", "urgent", "severity"]
        _assert_revised_schema(_run_declaration(revised))
        assert dict(revised.topology.step_interface_json) == current_wire.step_interface_json
        assert operator.get_run_result(revised.run_id) == "jev-test-resolved"

        workflow.unlink()
        operator._refresh_workflows()
        assert operator.list_workflows() == []
        retained = operator.get_latest_run_snapshot(
            original.run_id, operator_instance_id=operator.operator_instance_id
        )
        assert retained is not None
        historical = ClassifierDeclaration.model_validate_json(
            dict(retained.topology.classifier_metadata_json)[classifier_node(original)]
        )
        _assert_structured_declaration(historical)
        _assert_original_schema(historical)
        retained_topology = workflow_topology_from_v2(
            pb.WorkflowTopologyV2.FromString(
                workflow_topology_to_v2(retained.topology).SerializeToString()
            )
        )
        assert dict(retained_topology.step_interface_json) == original_interfaces
        records = read_invocations(operator, original)
        assert [record.status for record in records] == ["running", "success"]
        _assert_structured_declaration(records[-1].declaration)
        _assert_original_schema(records[-1].declaration)
        result = records[-1].result
        assert result is not None
        assert result.scores["severity"].legend == {
            "0": "minor",
            "1": {"impact": ["service unavailable"]},
        }
        assert result.choices["department"].choice == "billing"
    finally:
        operator.close()


@pytest.mark.usefixtures("typesafe_service")
def test_prepared_run_uses_executed_declaration_not_stale_catalog(tmp_path):
    workflow = _write_schema_workflow(tmp_path)
    operator = Operator([str(workflow)], executor_backend="local", watch=False, schedule=False)
    try:
        _write_schema_workflow(tmp_path, revised=True)
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        [catalog] = operator.list_workflows()
        _assert_structured_declaration(
            ClassifierDeclaration.model_validate_json(
                catalog.classifier_metadata_json[classifier_node(run)]
            )
        )
        declaration = _run_declaration(run)
        _assert_revised_schema(declaration)
        assert set(dict(run.topology.step_interface_json)) == set(catalog.step_interface_json)
        assert all(
            interface != catalog.step_interface_json[node_id]
            for node_id, interface in run.topology.step_interface_json
        )
        assert list(declaration.questions) == ["department", "urgent", "severity"]
        assert declaration.questions["urgent"].instructions == {
            "ask": ["Is this a production outage?"]
        }
        [completed] = [
            record for record in read_invocations(operator, run) if record.status == "success"
        ]
        _assert_revised_schema(completed.declaration)
        assert completed.declaration.questions["urgent"].instructions == {
            "ask": ["Is this a production outage?"]
        }
        assert completed.result is not None
        assert completed.result.scores["severity"].legend == {
            "0": "cosmetic",
            "1": {"impact": ["all customers blocked"]},
        }
    finally:
        operator.close()
