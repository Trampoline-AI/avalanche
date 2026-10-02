"""Safe discovery and immutable run declarations stay separate from evaluation results."""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from textwrap import dedent

import pytest

import avalanche as ava
from avalanche._evaluation_inputs import MetricInput
from avalanche.evaluations import EvaluationDeclaration
from runtime.operator import Operator, WorkflowRegistry
from runtime.operator.convert_v2 import (
    workflow_info_from_v2,
    workflow_info_to_v2,
    workflow_topology_from_v2,
    workflow_topology_to_v2,
)
from runtime.operator.models import RunStatus
from runtime.operator.operator import _CoordinatorProtocolError, _validate_preparation_event
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.registry import workflow_to_info
from runtime.operator.run_worker import _workflow_metadata

from .test_evaluation_execution import wait_terminal


def _questions(*, revised: bool = False):
    return {
        "grounded": {
            "type": "noul",
            "instructions": {
                "ask": ["Is the revised report supported?" if revised else "Is it supported?"]
            },
            "criteria": {"true": {"evidence": ["cited"]}, "false": None},
        },
        "clarity": {
            "type": "score",
            "instructions": ["Rate clarity", {"audience": "new reader"}],
            "criteria": ["unclear", {"level": "understandable"}, "precise"],
        },
        "route": {
            "type": "choice",
            "instructions": None,
            "criteria": {"accept": {"reason": ["supported"]}, "review": None},
        },
    }


def _declaration(*, revised: bool = False) -> EvaluationDeclaration:
    return EvaluationDeclaration.model_validate_json(
        json.dumps(
            {
                "metrics": _questions(revised=revised),
                "metric_inputs": {
                    name: [{"source": "custom", "selector": "forbidden"}]
                    for name in _questions(revised=revised)
                },
                "composites": ["revised_overall" if revised else "overall"],
                "runtime": {
                    "model": "workflow-revised" if revised else "workflow-original",
                    "timeout": 4 if revised else 3,
                },
            }
        )
    )


def _write_workflow(root: Path, *, revised: bool = False, guarded: bool = False) -> Path:
    workflow = root / "evaluation_metadata_flow.py"
    workflow.write_text(
        dedent(
            f"""
            import os
            import socket
            import typesafe_sdk
            import avalanche as ava

            if os.environ.get("EVALUATION_TEST_FORBID_IMPORT"):
                raise AssertionError("cached discovery must not import author code")

            def forbidden(*args, **kwargs):
                raise AssertionError("declaration discovery executed runtime code")

            if {guarded!r}:
                socket.socket.connect = forbidden
                typesafe_sdk.AsyncTypeSafeClient = forbidden
                ava.Agent.__init__ = forbidden

            evaluations = ava.Evaluations(
                metrics={{name: ava.Metric(state=forbidden, question=question)
                         for name, question in {_questions(revised=revised)!r}.items()}},
                composites={{{'revised_overall' if revised else 'overall'!r}: forbidden}},
                timeout={4 if revised else 3},
            )

            @ava.source
            def load() -> str:
                return "evidence"

            @ava.agent_step(ava.Signature("query: str -> reply: str"))
            def plain(value: str, *, agent: ava.Agent) -> str:
                return value

            @ava.agent_step(ava.Signature("query: str -> reply: str"), evaluations=evaluations)
            def research(value: str, *, agent: ava.Agent) -> str:
                return value

            @ava.workflow(classifier_defaults={{
                "model": {'workflow-revised' if revised else 'workflow-original'!r},
                "timeout": 9,
            }})
            def flow():
                return research(plain(load()))
            """
        )
    )
    return workflow


def test_discovery_cache_and_catalog_expose_evaluations_without_credentials_or_execution(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("EVALUATION_TEST_FORBID_IMPORT", raising=False)
    workflow = _write_workflow(tmp_path, guarded=True)
    cache_dir = tmp_path / "cache"
    registry = WorkflowRegistry(cache_dir=cache_dir)
    registry.scan([str(workflow)])
    assert registry.list_diagnostics() == []
    [descriptor] = registry.descriptors()
    [(node_id, metadata)] = descriptor.evaluation_metadata_json
    assert dict(descriptor.display_names)[node_id] == "research"
    assert EvaluationDeclaration.model_validate_json(metadata) == _declaration()

    # A fresh registry must reuse a complete validated declaration without another import.
    monkeypatch.setenv("EVALUATION_TEST_FORBID_IMPORT", "1")
    restarted = WorkflowRegistry(cache_dir=cache_dir)
    restarted.scan([str(workflow)])
    assert restarted.list_diagnostics() == []
    [catalog] = restarted.list_workflows()
    current_wire = pb.FlowInfoV2.FromString(workflow_info_to_v2(catalog).SerializeToString())
    current = workflow_info_from_v2(current_wire)
    assert current.evaluation_metadata_json == {node_id: metadata}
    assert dict(current_wire.topology.evaluation_metadata_json) == {node_id: metadata}
    assert set(current.display_names.values()) == {"load", "plain", "research"}

    # Public dicts and decoded nested values are not the immutable cached descriptor.
    exported = EvaluationDeclaration.model_validate_json(
        current.evaluation_metadata_json[node_id]
    )
    exported.metrics.clear()
    exported.metric_inputs.clear()
    catalog.evaluation_metadata_json.clear()
    current.evaluation_metadata_json.clear()
    [retained] = restarted.list_workflows()
    assert retained.evaluation_metadata_json == {node_id: metadata}
    assert dict(restarted.descriptors()[0].evaluation_metadata_json) == {node_id: metadata}


def test_pre_selector_cache_schema_is_rediscovered_instead_of_hiding_provenance(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    workflow = _write_workflow(tmp_path, guarded=True)
    cache_dir = tmp_path / "cache"
    registry = WorkflowRegistry(cache_dir=cache_dir)
    registry.scan([str(workflow)])
    [cache_file] = cache_dir.glob("*.json")
    cached = json.loads(cache_file.read_text())
    cached["schema_version"] = 8
    for result in cached["files"]:
        for descriptor in result["descriptors"]:
            older_metadata = []
            for node_id, metadata in descriptor["evaluation_metadata_json"]:
                declaration = json.loads(metadata)
                declaration.pop("metric_inputs")
                older_metadata.append([node_id, json.dumps(declaration)])
            descriptor["evaluation_metadata_json"] = older_metadata
    cache_file.write_text(json.dumps(cached))

    restarted = WorkflowRegistry(cache_dir=cache_dir)
    restarted.scan([str(workflow)])
    assert restarted.list_diagnostics() == []
    [catalog] = restarted.list_workflows()
    [metadata] = catalog.evaluation_metadata_json.values()
    assert EvaluationDeclaration.model_validate_json(metadata) == _declaration()


def test_historical_evaluations_use_prepared_source_and_survive_reload_and_removal(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    workflow = _write_workflow(tmp_path)
    operator = Operator([str(workflow)], executor_backend="local", watch=False, schedule=False)
    try:
        original = wait_terminal(operator, operator.start_run("flow"))
        assert original.status == RunStatus.SUCCESS
        [(node_id, original_json)] = original.topology.evaluation_metadata_json
        assert EvaluationDeclaration.model_validate_json(original_json) == _declaration()

        _write_workflow(tmp_path, revised=True)
        revised = wait_terminal(operator, operator.start_run("flow"))
        assert revised.status == RunStatus.SUCCESS
        [stale_catalog] = operator.list_workflows()
        assert stale_catalog.evaluation_metadata_json == {node_id: original_json}
        revised_json = dict(revised.topology.evaluation_metadata_json)[node_id]
        assert EvaluationDeclaration.model_validate_json(revised_json) == _declaration(
            revised=True
        )
        operator._refresh_workflows()
        [current] = operator.list_workflows()
        assert current.evaluation_metadata_json == {node_id: revised_json}

        workflow.unlink()
        operator._refresh_workflows()
        assert operator.list_workflows() == []
        retained = operator.get_latest_run_snapshot(
            original.run_id, operator_instance_id=operator.operator_instance_id
        )
        assert retained is not None
        wire = pb.WorkflowTopologyV2.FromString(
            workflow_topology_to_v2(retained.topology).SerializeToString()
        )
        assert dict(workflow_topology_from_v2(wire).evaluation_metadata_json) == {
            node_id: original_json
        }
        wire.ClearField("evaluation_metadata_json")
        assert workflow_topology_from_v2(wire).evaluation_metadata_json == ()
    finally:
        operator.close()


def _workflow():
    @ava.source
    def load() -> str:
        return "evidence"

    evaluations = ava.Evaluations(
        metrics={
            name: ava.Metric(state=lambda ctx: ctx.output, question=question)
            for name, question in _questions().items()
        },
        composites={"overall": lambda answers: 0.5},
        timeout=3,
    )

    @ava.agent_step(ava.Signature("query: str -> reply: str"), evaluations=evaluations)
    def research(value: str, *, agent: ava.Agent) -> str:
        return value

    @ava.workflow(classifier_defaults={"model": "workflow-original", "timeout": 9})
    def flow():
        return research(load())

    return flow()


def test_manual_catalog_and_prepared_topology_own_evaluation_declarations():
    workflow = _workflow()
    catalog = workflow_to_info(workflow, "flow.py")
    event = {"type": "prepared", **_workflow_metadata(workflow)}
    assert _validate_preparation_event(event) == "prepared"
    run = Operator._run_from_prepared("run", "flow", "flow", "manual", 100.0, event)
    [(node_id, metadata)] = run.topology.evaluation_metadata_json
    assert catalog.evaluation_metadata_json == {node_id: metadata}
    declaration = EvaluationDeclaration.model_validate_json(metadata)
    assert declaration.model_dump(exclude={"metric_inputs"}) == _declaration().model_dump(
        exclude={"metric_inputs"}
    )
    assert declaration.metric_inputs == {
        name: (MetricInput(source="output", selector=""),) for name in _questions()
    }
    event["evaluation_metadata_json"].clear()
    catalog.evaluation_metadata_json.clear()
    assert dict(run.topology.evaluation_metadata_json) == {node_id: metadata}


@pytest.mark.parametrize(
    "failure",
    [
        "invalid-json",
        "invalid-question",
        "coerced-timeout",
        "credential-field",
        "empty-metrics",
        "invalid-composites",
        "duplicate-composites",
        "unknown-node",
        "non-string-declaration",
        "non-mapping",
        "missing-map",
    ],
)
def test_preparation_rejects_malformed_or_unknown_evaluation_declarations(failure):
    event = {"type": "prepared", **_workflow_metadata(_workflow())}
    metadata = event["evaluation_metadata_json"]
    [node_id] = metadata
    declaration = json.loads(metadata[node_id])
    if failure == "invalid-json":
        metadata[node_id] = "{"
    elif failure == "invalid-question":
        declaration["metrics"]["grounded"]["type"] = "private-metadata-secret"
    elif failure == "coerced-timeout":
        declaration["runtime"]["timeout"] = "3"
    elif failure == "credential-field":
        declaration["runtime"]["api_key"] = "private-metadata-secret"
    elif failure == "empty-metrics":
        declaration["metrics"] = {}
    elif failure == "invalid-composites":
        declaration["composites"] = [""]
    elif failure == "duplicate-composites":
        declaration["composites"] = ["overall", "overall"]
    elif failure == "unknown-node":
        metadata["missing"] = metadata.pop(node_id)
    elif failure == "non-string-declaration":
        metadata[node_id] = declaration
    elif failure == "non-mapping":
        event["evaluation_metadata_json"] = []
    else:
        del event["evaluation_metadata_json"]
    if failure in {
        "invalid-question",
        "coerced-timeout",
        "credential-field",
        "empty-metrics",
        "invalid-composites",
        "duplicate-composites",
    }:
        metadata[node_id] = json.dumps(declaration)

    with pytest.raises(_CoordinatorProtocolError) as caught:
        _validate_preparation_event(event)
    assert "private-metadata-secret" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("limit", ["node", "total"])
def test_preparation_bounds_evaluation_declaration_bytes(monkeypatch, limit):
    event = {"type": "prepared", **_workflow_metadata(_workflow())}
    [declaration] = event["evaluation_metadata_json"].values()
    size = len(declaration.encode())
    if limit == "node":
        monkeypatch.setattr(
            "runtime.operator.operator._MAX_EVENT_EVALUATION_METADATA_BYTES", size - 1
        )
    else:
        event["evaluation_metadata_json"] = {
            node_id: declaration for node_id in event["node_ids"]
        }
        monkeypatch.setattr(
            "runtime.operator.operator._MAX_EVENT_EVALUATION_METADATA_TOTAL_BYTES", size * 2 - 1
        )
    with pytest.raises(_CoordinatorProtocolError):
        _validate_preparation_event(event)
