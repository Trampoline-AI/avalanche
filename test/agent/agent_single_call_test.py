"""An agent step calls its injected agent at most once per execution, on any runtime."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import ClassVar

import pytest
from pydantic import BaseModel

import avalanche as ava
from avalanche.agent import AgentStepCallError, AgentStepError
from avalanche.agent.contract import AgentRequest, AgentRunOutcome, JsonOutputs
from avalanche.agent.native.predict_rlm import (
    PredictRLMTokenUsage,
    PredictRLMTraceHeader,
    PredictRLMUsage,
)
from avalanche.agent.trace import AgentTrace, AgentTraceHeader

INVOCATIONS: list[str] = []
_NO_TOKENS = PredictRLMTokenUsage(input_tokens=0, output_tokens=0, cost=0.0, cache_hits=0)


class Echo(ava.Signature):
    """Echo the text."""

    text: str = ava.InputField()
    echoed: str = ava.OutputField()


class _EchoRuntime(BaseModel):
    """A runtime with no per-runtime call rule; it records each invocation it starts."""

    kind: ClassVar[str] = "echo"

    def merged_over(self, base):
        return self.model_copy()

    def declaration(self, *, workflow_default):
        raise NotImplementedError

    async def invoke(self, request: AgentRequest, events) -> AgentRunOutcome:
        INVOCATIONS.append(request.inputs.text)
        await asyncio.sleep(0)
        outputs = JsonOutputs(text=f'{{"echoed": "{request.inputs.text}"}}')
        trace = AgentTrace(
            header=AgentTraceHeader(
                status="succeeded",
                stop_reason="submit",
                model=None,
                duration_ms=1,
                usage=None,
                turn_count=0,
                tool_call_count=0,
                external_run_id=None,
                session_id=None,
                native=PredictRLMTraceHeader(
                    sub_model=None,
                    max_iterations=1,
                    usage=PredictRLMUsage(main=_NO_TOKENS, sub=_NO_TOKENS),
                    telemetry_ref=None,
                ),
            ),
            turns=(),
        )
        events.finished(trace.header, outputs=outputs, complete=True)
        return AgentRunOutcome(outputs=outputs, trace=trace)


@pytest.fixture(autouse=True)
def _reset_invocations():
    INVOCATIONS.clear()


async def _sequential(agent: ava.Agent) -> str:
    await agent(text="first")
    return (await agent(text="second")).outputs.echoed


async def _loop(agent: ava.Agent) -> str:
    answers = [(await agent(text=text)).outputs.echoed for text in ("a", "b", "c")]
    return ",".join(answers)


async def _concurrent(agent: ava.Agent) -> str:
    first, second = await asyncio.gather(agent(text="first"), agent(text="second"))
    return first.outputs.echoed + second.outputs.echoed


async def _after_failure(agent: ava.Agent) -> str:
    with pytest.raises(AgentStepError):
        await agent(unknown="field")
    return (await agent(text="retry")).outputs.echoed


@pytest.mark.filterwarnings("ignore:coroutine .* was never awaited:RuntimeWarning")
@pytest.mark.parametrize(
    ("body", "started"),
    [
        (_sequential, ["first"]),
        (_loop, ["a"]),
        # The second call is rejected as it is made, before gather starts either one.
        (_concurrent, []),
        # A failed call is still the step's one call.
        (_after_failure, []),
    ],
    ids=["sequential", "loop", "concurrent", "after-failed-call"],
)
def test_second_agent_call_fails_the_step_before_any_model_work(body, started):
    @ava.agent_step(Echo, runtime=_EchoRuntime())
    async def answer(*, agent: ava.Agent) -> str:
        return await body(agent)

    @ava.workflow
    def flow():
        return answer()

    with pytest.raises(AgentStepCallError, match="'answer'"):
        flow().run(executor=ava.LocalExecutor()).result(timeout=10)
    assert INVOCATIONS == started


def test_calls_from_a_synchronous_body_on_several_threads_run_one_agent():
    outcomes: list[str] = []

    @ava.agent_step(Echo, runtime=_EchoRuntime())
    def answer(*, agent: ava.Agent) -> list[str]:
        barrier = threading.Barrier(2)

        def call(text: str) -> None:
            barrier.wait()
            try:
                outcomes.append(asyncio.run(agent(text=text)).outputs.echoed)
            except AgentStepCallError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=call, args=(text,)) for text in ("x", "y")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return sorted(outcomes)

    @ava.workflow
    def flow():
        return answer()

    result = flow().run(executor=ava.LocalExecutor()).result(timeout=10)
    assert len(INVOCATIONS) == 1
    assert sorted(result) == sorted([INVOCATIONS[0], "rejected"])


def test_each_execution_of_a_step_may_call_its_agent_once():
    @ava.agent_step(Echo, runtime=_EchoRuntime())
    async def answer(text: str, *, agent: ava.Agent) -> str:
        return (await agent(text=text)).outputs.echoed

    @ava.workflow
    def flow():
        return answer("hello")

    # A rerun is a new execution with a fresh agent, also in the same process.
    for _ in range(2):
        assert flow().run(executor=ava.LocalExecutor()).result(timeout=10) == "hello"
    assert asyncio.run(answer.fn("direct")) == "direct"
    assert INVOCATIONS == ["hello", "hello", "direct"]


def test_inline_agent_calls_its_agent_once():
    @ava.workflow
    def flow():
        return ava.agent.step(Echo, inputs={"text": "inline"}, runtime=_EchoRuntime())

    assert flow().run(executor=ava.LocalExecutor()).result(timeout=10) == "inline"
    assert INVOCATIONS == ["inline"]


def test_step_that_never_calls_its_agent_returns_its_value_without_a_model_run(caplog):
    @ava.agent_step(Echo, runtime=_EchoRuntime())
    async def skip(*, agent: ava.Agent) -> str:
        return "skipped"

    @ava.workflow
    def flow():
        return skip()

    with caplog.at_level(logging.WARNING, logger="avalanche.agent.steps"):
        assert flow().run(executor=ava.LocalExecutor()).result(timeout=10) == "skipped"
    assert INVOCATIONS == []
    assert any(
        record.name == "avalanche.agent.steps" and record.levelno == logging.WARNING
        for record in caplog.records
    )


def test_step_that_raises_before_calling_its_agent_fails_without_the_no_call_warning(caplog):
    @ava.agent_step(Echo, runtime=_EchoRuntime())
    async def broken(*, agent: ava.Agent) -> str:
        raise ValueError("body failed")

    @ava.workflow
    def flow():
        return broken()

    with caplog.at_level(logging.WARNING, logger="avalanche.agent.steps"):
        with pytest.raises(ValueError, match="body failed"):
            flow().run(executor=ava.LocalExecutor()).result(timeout=10)
    assert INVOCATIONS == []
    assert not any(
        record.name == "avalanche.agent.steps" and record.levelno == logging.WARNING
        for record in caplog.records
    )


@pytest.mark.parametrize("inputs", [{"text": "hi"}, {"unknown": "field"}], ids=["ok", "failed"])
def test_called_reports_whether_the_one_call_was_used(inputs):
    seen: list[bool] = []

    @ava.agent_step(Echo, runtime=_EchoRuntime())
    async def answer(*, agent: ava.Agent) -> list[bool]:
        seen.append(agent.called)
        try:
            await agent(**inputs)
        except AgentStepError:
            pass
        seen.append(agent.called)
        return seen

    assert asyncio.run(answer.fn()) == [False, True]
