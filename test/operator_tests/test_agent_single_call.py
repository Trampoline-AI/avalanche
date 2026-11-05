"""A second agent call fails the step visibly in an operator-managed run."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from runtime.operator import Operator
from runtime.operator.models import NodeStatus, RunStatus

from .test_classifier_execution import wait_terminal


def _write_workflow(root: Path) -> Path:
    workflow = root / "twice_flow.py"
    workflow.write_text(
        dedent(
            f'''\
            from pathlib import Path
            from typing import ClassVar

            from pydantic import BaseModel

            import avalanche as ava
            from avalanche.agent.contract import AgentRunOutcome, JsonOutputs
            from avalanche.agent.native.predict_rlm import (
                PredictRLMTokenUsage,
                PredictRLMTraceHeader,
                PredictRLMUsage,
            )
            from avalanche.agent.trace import AgentTrace, AgentTraceHeader

            CALLS = Path({str(root / "calls.txt")!r})
            NO_TOKENS = PredictRLMTokenUsage(
                input_tokens=0, output_tokens=0, cost=0.0, cache_hits=0
            )

            class Echo(ava.Signature):
                """Echo the text."""
                text: str = ava.InputField()
                echoed: str = ava.OutputField()

            class EchoRuntime(BaseModel):
                kind: ClassVar[str] = "echo"

                def merged_over(self, base):
                    return self.model_copy()

                def declaration(self, *, workflow_default):
                    return ava.runtimes.PredictRLM().declaration(workflow_default=None)

                async def invoke(self, request, events):
                    with CALLS.open("a") as calls:
                        calls.write(request.inputs.text + "\\n")
                    outputs = JsonOutputs(text='{{"echoed": "ok"}}')
                    header = AgentTraceHeader(
                        status="succeeded", stop_reason="submit", model=None,
                        duration_ms=1, usage=None, turn_count=0, tool_call_count=0,
                        external_run_id=None, session_id=None,
                        native=PredictRLMTraceHeader(
                            sub_model=None, max_iterations=1,
                            usage=PredictRLMUsage(main=NO_TOKENS, sub=NO_TOKENS),
                            telemetry_ref=None,
                        ),
                    )
                    events.finished(header, outputs=outputs, complete=True)
                    return AgentRunOutcome(
                        outputs=outputs, trace=AgentTrace(header=header, turns=())
                    )

            @ava.agent_step(Echo, runtime=EchoRuntime(), retries=0)
            async def ask_twice(*, agent: ava.Agent) -> str:
                for text in ("first", "second"):
                    answer = await agent(text=text)
                return answer.outputs.echoed

            @ava.workflow
            def twice():
                return ask_twice()
            '''
        )
    )
    return workflow


def test_second_agent_call_fails_the_node_and_reaches_its_error(tmp_path):
    operator = Operator(
        [str(_write_workflow(tmp_path))],
        watch=False,
        schedule=False,
        state_db=":memory:",
    )
    try:
        run = wait_terminal(operator, operator.start_run("twice"))
        assert run.status == RunStatus.FAILED
        [node] = run.nodes.values()
        assert node.status == NodeStatus.FAILED
        assert "AgentStepCallError" in node.error
        assert "ask_twice" in node.error
        assert (tmp_path / "calls.txt").read_text().splitlines() == ["first"]
    finally:
        operator.close()
