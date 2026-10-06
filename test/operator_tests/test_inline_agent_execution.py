"""Inline agent discovery and execution through isolated operator processes."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from textwrap import dedent

import pytest

from avalanche.step_interface import StepInterface
from runtime.operator import Operator, WorkflowRegistry
from runtime.operator.convert_v2 import (
    workflow_info_from_v2,
    workflow_info_to_v2,
    workflow_topology_from_v2,
    workflow_topology_to_v2,
)
from runtime.operator.models import NodeStatus, RunStatus
from runtime.operator.operator import RunResultUnavailableError
from runtime.operator.proto import operator_pb2 as pb

from .test_classifier_execution import wait_terminal


def _write_workflow(root: Path, *, failure: str | None = None, block: bool = False) -> Path:
    """Install a test-only predictor in the child module, not in the parent process."""
    workflow = root / "inline_agent_flow.py"
    workflow.write_text(
        dedent(
            f'''\
            import asyncio
            import json
            import os
            from pathlib import Path
            import dspy
            import avalanche as ava
            import predict_rlm
            from pydantic import BaseModel, Field

            if os.environ.get("INLINE_AGENT_FORBID_IMPORT"):
                raise AssertionError("cached discovery reimported the workflow")

            CALLS = Path({str(root / "calls.jsonl")!r})
            FAILURE = {failure!r}
            BLOCK = {block!r}

            class Ticket(BaseModel):
                customer: str = Field(min_length=1)
                amount: int = Field(ge=0)

            class Decision(BaseModel):
                route: str = Field(min_length=1)
                priority: int = Field(ge=0)

            class Review(ava.Signature):
                """Route a customer ticket."""
                ticket: Ticket = ava.InputField()
                threshold: int = ava.InputField()
                decision: Decision = ava.OutputField()

            class Route(ava.Signature):
                """Score and route a customer ticket."""
                amount: int = ava.InputField()
                score: int = ava.OutputField(ge=0)
                route: str = ava.OutputField(min_length=1)

            class TestPredictor:
                def __init__(self, signature, *, skills=(), tools=(), events=(),
                             lm=None, sub_lm=None, max_iterations=8, verbose=False):
                    if os.environ.get("INLINE_AGENT_FORBID_EXECUTION"):
                        raise AssertionError("discovery constructed a predictor")
                    self.signature = signature
                    self.events = events
                    self.max_iterations = max_iterations
                    self.config = {{"lm": lm, "sub_lm": sub_lm,
                                   "max_iterations": max_iterations, "verbose": verbose}}

                async def acall(self, **inputs):
                    values = {{name: value.model_dump() if isinstance(value, BaseModel)
                              else value for name, value in inputs.items()}}
                    with CALLS.open("a") as stream:
                        stream.write(
                            json.dumps({{"inputs": values, "config": self.config}})
                            + "\\n"
                        )
                    started = predict_rlm.RunEvent(
                        "test-prediction", 1, predict_rlm.RunEventKind.RUN_STARTED,
                        1, {{"inputs": inputs}})
                    for sink in self.events:
                        await sink.emit(started)
                    if BLOCK:
                        try:
                            await asyncio.Event().wait()
                        except asyncio.CancelledError as error:
                            error.evidence = predict_rlm.RunEvidence(
                                run_id="test-prediction", complete=True,
                                terminal_outcome="cancelled",
                            )
                            raise
                    if FAILURE == "exception":
                        error = RuntimeError("ticket provider unavailable")
                        error.evidence = predict_rlm.RunEvidence(
                            run_id="test-prediction", complete=True, terminal_outcome="error",
                        )
                        raise error
                    if "decision" in self.signature.output_fields:
                        ticket = inputs["ticket"]
                        decision = Decision(
                            route="billing",
                            priority=ticket.amount // inputs["threshold"],
                        )
                        outputs = {{
                            "decision": None if FAILURE == "invalid-type" else decision,
                        }}
                    else:
                        outputs = {{
                            "score": (
                                -1 if FAILURE == "invalid-constraint"
                                else inputs["amount"] // 2
                            ),
                            "route": "billing",
                        }}
                    finished = predict_rlm.RunEvent(
                        "test-prediction", 2, predict_rlm.RunEventKind.RUN_SUCCEEDED,
                        2, {{"status": "completed", "outputs": outputs}})
                    for sink in self.events:
                        await sink.emit(finished)
                    return dspy.Prediction(
                        **outputs,
                        trace=predict_rlm.RunTrace(
                            status="completed", model="test-predictor", iterations=0,
                            max_iterations=self.max_iterations, duration_ms=1,
                        ),
                        evidence=predict_rlm.RunEvidence(
                            run_id="test-prediction", complete=True,
                            terminal_outcome="completed",
                        ),
                    )

            # This replacement exists only in the temporary module's isolated child.
            predict_rlm.PredictRLM = TestPredictor

            @ava.source
            def load_ticket() -> Ticket:
                if os.environ.get("INLINE_AGENT_FORBID_EXECUTION"):
                    raise AssertionError("discovery executed a source")
                return Ticket(customer="Ada", amount=12)

            @ava.source
            def load_amount() -> int:
                if os.environ.get("INLINE_AGENT_FORBID_EXECUTION"):
                    raise AssertionError("discovery executed a source")
                return 12

            @ava.step
            def consume_decision(decision: Decision) -> dict[str, str | int]:
                return {{"queue": decision.route, "priority": decision.priority + 1}}

            @ava.step
            def consume_route(score: int, route: str) -> dict[str, str | int]:
                return {{"queue": route, "priority": score + 1}}

            DEFAULTS = {{"lm": "workflow-model", "sub_lm": "workflow-submodel",
                        "max_iterations": 4, "verbose": True}}

            @ava.workflow(agent_defaults=DEFAULTS)
            def single():
                reviewed = ava.agent.step(
                    Review, inputs={{"ticket": load_ticket(), "threshold": 2}},
                    slug="review-ticket", lm="inline-model", max_iterations=2)
                return consume_decision(reviewed)

            @ava.workflow(agent_defaults=DEFAULTS)
            def multiple():
                routed = ava.agent.step(Route, slug="route-ticket")
                load_amount() >> routed
                return consume_route(routed[0], routed[1])
            '''
        )
    )
    return workflow


def _interface(metadata, node_id: str) -> StepInterface:
    return StepInterface.model_validate_json(dict(metadata)[node_id])


def _assert_review_interface(interface: StepInterface) -> None:
    assert [(item.name, item.required) for item in interface.step_inputs] == [
        ("ticket", True),
        ("threshold", True),
    ]
    ticket_schema = interface.step_inputs[0].json_schema
    assert ticket_schema["properties"]["customer"]["minLength"] == 1
    assert ticket_schema["properties"]["amount"]["minimum"] == 0
    assert interface.step_inputs[1].json_schema == {"type": "integer"}
    output_schema = interface.step_output.json_schema
    assert output_schema["properties"]["route"]["minLength"] == 1
    assert output_schema["properties"]["priority"]["minimum"] == 0


def test_discovery_and_cached_current_metadata_do_not_execute_inline_agents(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("INLINE_AGENT_FORBID_EXECUTION", "1")
    monkeypatch.delenv("INLINE_AGENT_FORBID_IMPORT", raising=False)
    workflow = _write_workflow(tmp_path)
    before_modules = {
        name: module
        for name, module in sys.modules.items()
        if (module_file := getattr(module, "__file__", None)) is not None
        and Path(module_file).resolve() == workflow.resolve()
    }
    registry = WorkflowRegistry(cache_dir=tmp_path / "cache")
    registry.scan([str(workflow)])
    assert registry.list_diagnostics() == []
    descriptors = {item.display_name: item for item in registry.descriptors()}
    single = descriptors["single"]
    [agent_id] = single.agent_node_ids
    assert dict(single.node_types)[agent_id] == "step"
    declaration = json.loads(dict(single.agent_metadata_json)[agent_id])
    assert [field["name"] for field in declaration["signature"]["inputs"]] == [
        "ticket",
        "threshold",
    ]
    assert [field["name"] for field in declaration["signature"]["outputs"]] == ["decision"]
    assert declaration["runtime"]["max_iterations"] == 2
    assert declaration["runtime"]["verbose"] is True
    assert declaration["models"] == {
        "main": {"source": "step override", "identity": "inline-model"},
        "sub": {"source": "workflow default", "identity": "workflow-submodel"},
    }
    _assert_review_interface(_interface(single.step_interface_json, agent_id))

    monkeypatch.setenv("INLINE_AGENT_FORBID_IMPORT", "1")
    restarted = WorkflowRegistry(cache_dir=tmp_path / "cache")
    restarted.scan([str(workflow)])
    assert restarted.list_diagnostics() == []
    current = {item.name: item for item in restarted.list_workflows()}["single"]
    wire = workflow_info_from_v2(
        pb.FlowInfoV2.FromString(workflow_info_to_v2(current).SerializeToString())
    )
    assert wire.agent_node_ids == [agent_id]
    assert json.loads(wire.agent_metadata_json[agent_id])["models"] == declaration["models"]
    assert json.loads(wire.agent_metadata_json[agent_id])["runtime"]["max_iterations"] == 2
    _assert_review_interface(_interface(wire.step_interface_json, agent_id))
    assert not (tmp_path / "calls.jsonl").exists()
    assert {
        name: module
        for name, module in sys.modules.items()
        if (module_file := getattr(module, "__file__", None)) is not None
        and Path(module_file).resolve() == workflow.resolve()
    } == before_modules


@pytest.mark.parametrize("flow_name", ["single", "multiple"])
def test_spawned_inline_agent_feeds_typed_downstream_result_and_retains_evidence(
    tmp_path, flow_name
):
    workflow = _write_workflow(tmp_path)
    operator = Operator([str(workflow)], executor_backend="local", watch=False, schedule=False)
    try:
        current = {item.name: item for item in operator.list_workflows()}[flow_name]
        [agent_id] = current.agent_node_ids
        run = wait_terminal(operator, operator.start_run(flow_name))
        assert run.status == RunStatus.SUCCESS
        assert all(node.status == NodeStatus.SUCCESS for node in run.nodes.values())
        assert operator.get_run_result(run.run_id) == {"queue": "billing", "priority": 7}
        calls = [
            json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()
        ]
        expected_inputs = (
            {"ticket": {"customer": "Ada", "amount": 12}, "threshold": 2}
            if flow_name == "single"
            else {"amount": 12}
        )
        assert [call["inputs"] for call in calls] == [expected_inputs]
        assert calls[0]["config"] == {
            "lm": "inline-model" if flow_name == "single" else "workflow-model",
            "sub_lm": "workflow-submodel",
            "max_iterations": 2 if flow_name == "single" else 4,
            "verbose": True,
        }
        assert run.nodes[agent_id].node_type == "step"
        historical = workflow_topology_from_v2(
            pb.WorkflowTopologyV2.FromString(
                workflow_topology_to_v2(run.topology).SerializeToString()
            )
        )
        schemas = json.loads(dict(historical.agent_field_schemas_json)[agent_id])
        assert [field["name"] for field in schemas["inputs"]] == list(expected_inputs)
        expected_outputs = ["decision"] if flow_name == "single" else ["score", "route"]
        assert [field["name"] for field in schemas["outputs"]] == expected_outputs
        if flow_name == "single":
            _assert_review_interface(_interface(historical.step_interface_json, agent_id))
        else:
            output_schema = _interface(
                historical.step_interface_json, agent_id
            ).step_output.json_schema
            assert output_schema["minItems"] == output_schema["maxItems"] == 2
            assert [item["type"] for item in output_schema["prefixItems"]] == [
                "integer",
                "string",
            ]
        page = operator.list_agent_events(run.run_id, agent_id)
        bodies = [json.loads(operator.read_detail(event.body_token)) for event in page.events]
        assert [body["event_kind"] for body in bodies] == ["run.started", "run.succeeded"]
        assert bodies[0]["data"]["inputs"] == expected_inputs
        assert bodies[1]["data"]["outputs"] == (
            {"decision": {"route": "billing", "priority": 6}}
            if flow_name == "single"
            else {"score": 6, "route": "billing"}
        )
        assert len({event.invocation_id for event in page.events}) == 1
        trace = json.loads(operator.get_run(run.run_id).nodes[agent_id].agent_trace_json)
        assert trace["trace"]["status"] == "completed"
        assert trace["trace"]["max_iterations"] == calls[0]["config"]["max_iterations"]
        assert dict(historical.agent_instruction_lines)[agent_id] == (
            "Route a customer ticket."
            if flow_name == "single"
            else "Score and route a customer ticket."
        )
    finally:
        operator.close()


@pytest.mark.parametrize(
    ("flow_name", "failure"),
    [("single", "invalid-type"), ("multiple", "invalid-constraint"), ("single", "exception")],
)
def test_spawned_inline_agent_failure_never_publishes_partial_result(
    tmp_path, flow_name, failure
):
    workflow = _write_workflow(tmp_path, failure=failure)
    operator = Operator([str(workflow)], executor_backend="local", watch=False, schedule=False)
    try:
        current = {item.name: item for item in operator.list_workflows()}[flow_name]
        [agent_id] = current.agent_node_ids
        run = wait_terminal(operator, operator.start_run(flow_name))
        assert run.status == RunStatus.FAILED
        assert run.nodes[agent_id].status == NodeStatus.FAILED
        consumer_name = "consume_decision" if flow_name == "single" else "consume_route"
        [consumer] = [node for node in run.nodes.values() if node.name == consumer_name]
        assert consumer.status != NodeStatus.SUCCESS
        assert run.nodes[agent_id].error
        calls = [
            json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()
        ]
        assert len(calls) == 1
        with pytest.raises(RunResultUnavailableError):
            operator.get_run_result(run.run_id)
        events = operator.list_agent_events(run.run_id, agent_id).events
        assert events[0].event_kind == "run.started"
    finally:
        operator.close()


def test_spawned_running_inline_agent_cancellation_never_publishes_partial_result(tmp_path):
    workflow = _write_workflow(tmp_path, block=True)
    operator = Operator(
        [str(workflow)],
        executor_backend="local",
        watch=False,
        schedule=False,
        cancel_grace=0.2,
    )
    try:
        current = {item.name: item for item in operator.list_workflows()}["single"]
        [agent_id] = current.agent_node_ids
        run_id = operator.start_run("single")
        deadline = time.monotonic() + 20
        while agent_id not in operator.get_run(run_id).nodes:
            assert time.monotonic() < deadline, "coordinator did not publish the run topology"
            time.sleep(0.03)
        while time.monotonic() < deadline:
            events = operator.list_agent_events(run_id, agent_id).events
            if any(event.event_kind == "run.started" for event in events):
                break
            time.sleep(0.03)
        else:
            pytest.fail("inline agent did not emit run.started before cancellation")
        running = operator.get_run(run_id)
        assert running.status == RunStatus.RUNNING
        assert running.nodes[agent_id].status == NodeStatus.RUNNING

        operator.cancel_run(run_id)
        cancelled = wait_terminal(operator, run_id)
        assert cancelled.status == RunStatus.CANCELLED
        assert cancelled.nodes[agent_id].status not in {
            NodeStatus.RUNNING,
            NodeStatus.SUCCESS,
        }
        [consumer] = [
            node for node in cancelled.nodes.values() if node.name == "consume_decision"
        ]
        assert consumer.status != NodeStatus.SUCCESS
        assert not any(
            event.event_kind == "run.succeeded"
            for event in operator.list_agent_events(run_id, agent_id).events
        )
        with pytest.raises(RunResultUnavailableError):
            operator.get_run_result(run_id)
    finally:
        operator.close()
