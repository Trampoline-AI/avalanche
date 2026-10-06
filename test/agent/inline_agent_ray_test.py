"""Optional full RayExecutor dataflow with deterministic worker-local predictors."""

from __future__ import annotations

import importlib
import os
import sys

import dspy
import pytest
from pydantic import BaseModel

import avalanche as ava

pytestmark = pytest.mark.ray


class WorkerItems(BaseModel):
    customer: str
    names: list[str]
    quantities: list[int]
    worker_pid: int


class ExtractWorkerItems(ava.Signature):
    transcript: str = ava.InputField()
    customer: str = ava.InputField()
    items: WorkerItems = ava.OutputField()


class SummarizeWorkerItems(ava.Signature):
    items: WorkerItems = ava.InputField()
    total: int = ava.OutputField()
    label: str = ava.OutputField()
    extraction_pid: int = ava.OutputField()
    prediction_pid: int = ava.OutputField()


class CollectWorkerNames(ava.Signature):
    items: WorkerItems = ava.InputField()
    names: list[str] = ava.OutputField()


class DeterministicWorkerPredictor:
    def __init__(self, signature, *, skills, tools):
        self.signature = signature
        self.skills = skills
        self.tools = tools

    async def acall(self, **inputs):
        if tuple(self.signature.input_fields) == ("transcript", "customer"):
            names, quantities = self.skills[0].tools["parse_orders"](inputs["transcript"])
            return dspy.Prediction(
                items=WorkerItems(
                    customer=inputs["customer"],
                    names=names,
                    quantities=quantities,
                    worker_pid=os.getpid(),
                )
            )
        items = inputs["items"]
        if tuple(self.signature.output_fields) == ("names",):
            return dspy.Prediction(names=items.names)
        return dspy.Prediction(
            total=self.tools[0](items.quantities),
            label=items.customer + ":" + "/".join(items.names),
            extraction_pid=items.worker_pid,
            prediction_pid=os.getpid(),
        )


def _build_worker_predictor(signature, **config):
    return DeterministicWorkerPredictor(
        signature, skills=config["skills"], tools=config["tools"]
    )


def _install_worker_predictor():
    # This setup is restricted to this remote driver's runtime environment; its
    # child Ray tasks inherit that environment. The pytest driver is untouched.
    module = importlib.import_module("avalanche.agent.agent_step")
    module._build_predictor = _build_worker_predictor


@pytest.fixture(scope="module")
def ray_runtime():
    ray = pytest.importorskip("ray")
    owns_runtime = not ray.is_initialized()
    if owns_runtime:
        ray.init(num_cpus=2, include_dashboard=False)
    yield ray
    if owns_runtime:
        ray.shutdown()


def test_full_inline_ray_workflow_returns_validated_values_from_real_workers(ray_runtime):
    from ray import cloudpickle
    from ray._private.runtime_env.setup_hook import export_setup_func_module

    agent_module = importlib.import_module("avalanche.agent.agent_step")
    driver_builder = agent_module._build_predictor

    def parse_orders(transcript: str) -> tuple[list[str], list[int]]:
        orders = [entry.split() for entry in transcript.split(",")]
        return (
            [name for quantity, name in orders],
            [int(quantity) for quantity, name in orders],
        )

    def count_items(quantities: list[int]) -> int:
        return sum(quantities)

    order_skill = ava.agent.Skill(
        name="order-extraction",
        instructions="Parse comma-separated quantity/name orders with parse_orders.",
        tools={"parse_orders": parse_orders},
    )

    @ava.source
    def recording():
        return "2 apples,3 pears"

    @ava.source
    def account():
        return "Ada"

    @ava.dest
    def render(
        total: int, label: str, extraction_pid: int, prediction_pid: int, names: list[str]
    ):
        return {
            "receipt": f"{label}={total}",
            "names": names,
            "extraction_pid": extraction_pid,
            "prediction_pid": prediction_pid,
            "render_pid": os.getpid(),
        }

    @ava.workflow
    def flow():
        extracted = ava.agent.step(ExtractWorkerItems, skills=[order_skill])
        (recording() & account()) >> extracted
        summary = ava.agent.step(
            SummarizeWorkerItems, inputs={"items": extracted}, tools=[count_items]
        )
        names = ava.agent.step(CollectWorkerNames, inputs={"items": extracted})
        return (summary & names) >> render()

    # Export the test module's import path to workers so the deterministic setup
    # is importable, including under pytest's prepend import mode.
    runtime_env = {
        "env_vars": {"PYTHONPATH": os.pathsep.join(os.path.abspath(path) for path in sys.path)},
        "worker_process_setup_hook": f"{__name__}._install_worker_predictor",
    }
    # Ray exports setup hooks during ray.init(), not per-task runtime-env
    # serialization. Export the importable hook into this task's worker env;
    # nested RayExecutor tasks inherit it without changing the pytest driver.
    export_setup_func_module(runtime_env, runtime_env["worker_process_setup_hook"])

    @ray_runtime.remote(num_cpus=0, runtime_env=runtime_env)
    def execute(serialized_workflow):
        import os

        from ray import cloudpickle

        import avalanche as ava

        workflow = cloudpickle.loads(serialized_workflow)
        result = workflow.run(executor=ava.RayExecutor()).result(timeout=30)
        return os.getpid(), result

    worker_driver_pid, result = ray_runtime.get(
        execute.remote(cloudpickle.dumps(flow())), timeout=60
    )
    assert worker_driver_pid != os.getpid()
    assert result["receipt"] == "Ada:apples/pears=5"
    assert result["names"] == ["apples", "pears"]
    assert result["extraction_pid"] != os.getpid()
    assert result["prediction_pid"] != os.getpid()
    assert result["render_pid"] != os.getpid()
    assert result["extraction_pid"] != worker_driver_pid
    assert result["prediction_pid"] != worker_driver_pid
    assert result["render_pid"] != worker_driver_pid
    assert agent_module._build_predictor is driver_builder
