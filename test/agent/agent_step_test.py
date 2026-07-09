"""Tests for bodyful ``@ava.agent_step`` workflow nodes."""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

import avalanche as ava
from avalanche.agent import AgentStepError, AgentStepExecutionError
from avalanche.executor import LocalExecutor

agent_step_module = importlib.import_module("avalanche.agent.agent_step")


class Person(BaseModel):
    id: int
    name: str


class Summary(BaseModel):
    headline: str
    person_count: int


class SummarySignature(ava.Signature):
    """Summarize the supplied person."""

    person: Person = ava.InputField(desc="person to summarize")
    summary: Summary = ava.OutputField(desc="structured summary")
    note: str = ava.OutputField(desc="review note")


class FakePredictor:
    """Deterministic PredictRLM substitute behind the lazy construction seam."""

    def __init__(self, respond):
        self.respond = respond
        self.calls: list[dict[str, Any]] = []

    async def acall(self, **inputs: Any) -> Any:
        self.calls.append(inputs)
        return self.respond(inputs)


def install_fake(monkeypatch, respond):
    """Install a fake predictor while retaining the public Agent call boundary."""
    captured = {"builds": [], "predictors": []}

    def fake_build(signature, *, skills, tools, **runtime_kwargs):
        captured["builds"].append(
            {
                "signature": signature,
                "skills": skills,
                "tools": tools,
                "runtime_kwargs": runtime_kwargs,
            }
        )
        predictor = FakePredictor(respond)
        captured["predictors"].append(predictor)
        return predictor

    monkeypatch.setattr(agent_step_module, "_build_predictor", fake_build)
    return captured


class TestBodyfulAgentSteps:
    def test_body_receives_callable_agent_and_owns_multi_output_handling(self, monkeypatch):
        """A step body receives the raw prediction and decides its public result."""
        captured = install_fake(
            monkeypatch,
            lambda inputs: SimpleNamespace(
                summary=Summary(
                    headline=f"about {inputs['person'].name}",
                    person_count=1,
                ),
                note="ready for review",
            ),
        )

        @ava.step
        def load() -> Person:
            return Person(id=1, name="Ada")

        @ava.agent.step(SummarySignature)
        async def summarize(person: Person, *, agent: ava.Agent) -> dict[str, str]:
            prediction = await agent(person=person)
            return {
                "headline": prediction.summary.headline,
                "note": prediction.note,
            }

        @ava.workflow
        def flow():
            return summarize(load())

        person = Person(id=1, name="Ada")
        assert flow().run(executor=LocalExecutor()) == {
            "headline": "about Ada",
            "note": "ready for review",
        }
        assert captured["predictors"][0].calls == [{"person": person}]
        resolved = captured["builds"][0]["signature"]
        assert list(resolved.input_fields) == ["person"]
        assert list(resolved.output_fields) == ["summary", "note"]

    def test_root_agent_step_alias_accepts_positional_signature(self, monkeypatch):
        """The restored root decorator remains a bodyful Agent step."""
        captured = install_fake(
            monkeypatch,
            lambda _inputs: SimpleNamespace(
                summary=Summary(headline="about Ada", person_count=1),
                note="ready for review",
            ),
        )

        @ava.agent_step(SummarySignature, lm="root-lm")
        async def summarize(person: Person, *, agent: ava.Agent) -> str:
            return (await agent(person=person)).note

        @ava.workflow
        def flow():
            return summarize(Person(id=1, name="Ada"))

        assert flow().run(executor=LocalExecutor()) == "ready for review"
        assert ava.agent_step is ava.agent.agent_step is ava.agent.step
        assert captured["builds"][0]["runtime_kwargs"] == {"lm": "root-lm"}

    def test_dynamic_signature_capabilities_are_inherited_without_overrides(self, monkeypatch):
        """An inline Signature's declared capabilities reach PredictRLM unchanged."""
        signature_skill = object()

        def signature_tool(text: str) -> str:
            return text

        signature = ava.agent.Signature(
            "text: str -> verdict: str",
            "Classify the supplied text.",
            skills=[signature_skill],
            tools=[signature_tool],
        )
        captured = install_fake(
            monkeypatch,
            lambda inputs: SimpleNamespace(verdict=inputs["text"].upper()),
        )

        @ava.agent.step(signature)
        async def classify(text: str, *, agent: ava.Agent) -> str:
            return (await agent(text=text)).verdict

        @ava.workflow
        def flow():
            return classify("clear")

        assert flow().run(executor=LocalExecutor()) == "CLEAR"
        build = captured["builds"][0]
        assert build["skills"] == (signature_skill,)
        assert build["tools"] == (signature_tool,)
        assert list(build["signature"].input_fields) == ["text"]
        assert list(build["signature"].output_fields) == ["verdict"]

    def test_decorator_capabilities_replace_signature_capabilities(self, monkeypatch):
        """Explicit decorator capabilities replace rather than merge Signature ones."""
        signature_skill = object()
        decorator_skill = object()

        def signature_tool(text: str) -> str:
            return text

        def decorator_tool(text: str) -> str:
            return text.upper()

        signature = ava.agent.Signature(
            "text: str -> verdict: str",
            "Classify the supplied text.",
            skills=[signature_skill],
            tools=[signature_tool],
        )
        captured = install_fake(
            monkeypatch,
            lambda inputs: SimpleNamespace(verdict=inputs["text"].upper()),
        )

        @ava.agent_step(
            signature,
            skills=[decorator_skill],
            tools=[decorator_tool],
        )
        async def classify(text: str, *, agent: ava.Agent) -> str:
            return (await agent(text=text)).verdict

        @ava.workflow
        def flow():
            return classify("clear")

        assert flow().run(executor=LocalExecutor()) == "CLEAR"
        build = captured["builds"][0]
        assert build["skills"] == (decorator_skill,)
        assert build["tools"] == (decorator_tool,)
        assert signature_skill not in build["skills"]
        assert signature_tool not in build["tools"]

    def test_agent_rejects_missing_and_unexpected_model_inputs(self):
        """Invalid model-call input names surface a precise boundary error."""

        @ava.agent.step(SummarySignature)
        async def summarize(*, agent: ava.Agent) -> str:
            return (await agent(extra="not in signature")).summary.headline

        @ava.workflow
        def flow():
            return summarize()

        with pytest.raises(
            AgentStepError,
            match=r"missing input fields \['person'\].*unexpected input fields \['extra'\]",
        ):
            flow().run(executor=LocalExecutor())

    def test_predictor_failure_includes_step_signature_and_input_types(self, monkeypatch):
        """Provider errors surface the step boundary needed to diagnose a failed call."""

        def fail(_inputs):
            raise ValueError("provider unavailable")

        install_fake(monkeypatch, fail)

        @ava.agent.step(SummarySignature)
        async def summarize(person: Person, *, agent: ava.Agent) -> str:
            return (await agent(person=person)).summary.headline

        @ava.workflow
        def flow():
            return summarize(Person(id=1, name="Ada"))

        with pytest.raises(AgentStepExecutionError) as error:
            flow().run(executor=LocalExecutor())

        message = str(error.value)
        assert "summarize" in message
        assert "person" in message
        assert "summary" in message
        assert "Person" in message
        assert "provider unavailable" in message


@pytest.mark.parametrize(
    ("name", "defaults"),
    [
        ("skills", {"skills": [object()]}),
        ("tools", {"tools": [lambda text: text]}),
    ],
)
def test_workflow_agent_defaults_reject_capabilities(name, defaults):
    """Workflow defaults cannot own model capabilities."""
    with pytest.raises(TypeError, match=name):

        @ava.workflow(agent_defaults=defaults)
        def flow():
            return "unreachable"


@pytest.mark.parametrize(
    ("name", "declare", "message"),
    [
        (
            "missing injected parameter",
            lambda signature: _decorate_without_agent(signature),
            "requires a keyword-only agent",
        ),
        (
            "positional injected parameter",
            lambda signature: _decorate_positional_agent(signature),
            "must be keyword-only",
        ),
        (
            "defaulted injected parameter",
            lambda signature: _decorate_defaulted_agent(signature),
            "cannot have a default",
        ),
        (
            "wrong injected annotation",
            lambda signature: _decorate_wrongly_annotated_agent(signature),
            "must be annotated",
        ),
    ],
)
def test_step_declaration_requires_framework_injected_agent(name, declare, message):
    """Only a required, keyword-only ``ava.Agent`` can be injected."""
    with pytest.raises(AgentStepError, match=message):
        declare(SummarySignature)


def _decorate_without_agent(signature):
    @ava.agent.step(signature)
    async def summarize(person: Person) -> str:
        return person.name


def _decorate_positional_agent(signature):
    @ava.agent.step(signature)
    async def summarize(agent: ava.Agent, person: Person) -> str:
        return person.name


def _decorate_defaulted_agent(signature):
    @ava.agent.step(signature)
    async def summarize(person: Person, *, agent: ava.Agent = None) -> str:
        return person.name


def _decorate_wrongly_annotated_agent(signature):
    @ava.agent.step(signature)
    async def summarize(person: Person, *, agent: object) -> str:
        return person.name
