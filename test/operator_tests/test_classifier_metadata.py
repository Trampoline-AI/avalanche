"""Discovery and historical declarations remain usable without executing classifiers."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from avalanche.classifier.models import ClassificationResult, ClassifierDeclaration
from runtime.operator import Operator, WorkflowRegistry
from runtime.operator.models import RunState, RunStatus

from .test_classifier_execution import (
    QUESTIONS,
    classifier_node,
    read_invocations,
    wait_terminal,
    write_workflow,
)
from .test_classifier_execution import (
    typesafe_service as typesafe_service,
)


def _write_discovery_workflow(root: Path) -> Path:
    workflow = root / "discover_classifier.py"
    workflow.write_text(
        "import os\n"
        "import socket\n"
        "import avalanche as ava\n"
        "if os.environ.get('CLASSIFIER_TEST_FORBID_IMPORT'):\n"
        "    raise RuntimeError('cached discovery must not reimport this module')\n"
        "def forbidden_connection(*args, **kwargs):\n"
        "    raise AssertionError('discovery attempted a network connection')\n"
        "socket.socket.connect = forbidden_connection\n"
        f"@ava.classifier_step(questions={QUESTIONS!r}, timeout=3.0)\n"
        "async def classify(*, classifier: ava.Classifier):\n"
        "    raise AssertionError('discovery executed the classifier body')\n"
        "@ava.workflow(classifier_defaults={'model': 'discovery-model', 'timeout': 6.0})\n"
        "def discoverable():\n"
        "    return classify()\n"
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


@pytest.mark.usefixtures("typesafe_service")
def test_historical_questions_and_answers_survive_reload_and_source_removal(tmp_path):
    body = "return await classifier(state=ticket)"
    workflow = write_workflow(tmp_path, body)
    operator = Operator([str(workflow)], executor_backend="local", watch=False, schedule=False)
    try:
        original = wait_terminal(operator, operator.start_run("flow"))
        assert original.status == RunStatus.SUCCESS
        _assert_structured_declaration(_run_declaration(original))

        write_workflow(tmp_path, body, questions=_revised_questions())
        operator._refresh_workflows()
        [current] = operator.list_workflows()
        current_declaration = ClassifierDeclaration.model_validate_json(
            current.classifier_metadata_json[classifier_node(original)]
        )
        assert list(current_declaration.questions) == ["department", "urgent", "severity"]
        assert current_declaration.questions["urgent"].instructions == {
            "ask": ["Is this a production outage?"]
        }
        revised = wait_terminal(operator, operator.start_run("flow"))
        assert revised.status == RunStatus.SUCCESS
        assert list(_run_declaration(revised).questions) == ["department", "urgent", "severity"]
        revised_result = ClassificationResult.model_validate(
            operator.get_run_result(revised.run_id)
        )
        assert revised_result.scores["severity"].legend == {
            "0": "cosmetic",
            "1": {"impact": ["all customers blocked"]},
        }

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
        records = read_invocations(operator, original)
        assert [record.status for record in records] == ["running", "success"]
        _assert_structured_declaration(records[-1].declaration)
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
    body = "return await classifier(state=ticket)"
    workflow = write_workflow(tmp_path, body)
    operator = Operator([str(workflow)], executor_backend="local", watch=False, schedule=False)
    try:
        write_workflow(tmp_path, body, questions=_revised_questions())
        run = wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        [catalog] = operator.list_workflows()
        _assert_structured_declaration(
            ClassifierDeclaration.model_validate_json(
                catalog.classifier_metadata_json[classifier_node(run)]
            )
        )
        declaration = _run_declaration(run)
        assert list(declaration.questions) == ["department", "urgent", "severity"]
        assert declaration.questions["urgent"].instructions == {
            "ask": ["Is this a production outage?"]
        }
        [completed] = [
            record for record in read_invocations(operator, run) if record.status == "success"
        ]
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
