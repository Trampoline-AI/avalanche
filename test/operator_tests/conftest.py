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


@pytest.fixture
def agent_evidence():
    """Wire bodies for a recorded PredictRLM run, made by the real adapter.

    Each step becomes a live ``turn.finished`` event, then the final trace.
    """
    from predict_rlm import RunEvent, RunEventKind, RunEvidence

    from avalanche.runtimes.predict_rlm.replay import replay

    def build(trace, *, invocation_id="invocation", run_id="sdk-run", complete=True):
        events = [
            RunEvent(run_id, index, RunEventKind.ITERATION_RECORDED, index, {"step": step})
            for index, step in enumerate(trace.steps, start=1)
        ]
        evidence = RunEvidence(run_id=run_id, complete=complete, terminal_outcome="completed")
        return replay(events, evidence, invocation_id=invocation_id, trace=trace)

    return build
