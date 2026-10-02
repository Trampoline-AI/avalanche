"""SDK model factories for agent transport scenarios."""

import pytest


@pytest.fixture
def iteration_step():
    from predict_rlm.trace import IterationStep

    def build(iteration, code):
        return IterationStep(
            iteration=iteration,
            reasoning=f"Reasoning for iteration {iteration}",
            code=code,
            output=f"Output for iteration {iteration}",
            untruncated_output=f"Full output for iteration {iteration}",
            duration_ms=iteration * 10,
        )

    return build


@pytest.fixture
def agent_trace():
    from predict_rlm import RunTrace

    def build(*, model="test-model", steps=(), telemetry_ref=None):
        return RunTrace(
            status="completed",
            model=model,
            sub_model="test-sub-model",
            iterations=len(steps),
            max_iterations=10,
            duration_ms=100,
            telemetry_ref=telemetry_ref,
            steps=list(steps),
        )

    return build
