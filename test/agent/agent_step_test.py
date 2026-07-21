"""Tests for bodyful ``@ava.agent_step`` workflow nodes."""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

import avalanche as ava
from avalanche._agent_evidence import capture_agent_evidence
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
        assert flow().run(executor=LocalExecutor()).result() == {
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

        assert flow().run(executor=LocalExecutor()).result() == "ready for review"
        assert ava.agent_step is ava.agent.agent_step is ava.agent.step
        assert captured["builds"][0]["runtime_kwargs"] == {"lm": "root-lm"}

    def test_decorator_capabilities_reach_predictor(self, monkeypatch):
        """Decorator capabilities reach the predictor constructed for the step."""
        skill = object()

        def tool(text: str) -> str:
            return text.upper()

        signature = ava.agent.Signature(
            "text: str -> verdict: str",
            "Classify the supplied text.",
        )
        captured = install_fake(
            monkeypatch,
            lambda inputs: SimpleNamespace(verdict=inputs["text"].upper()),
        )

        @ava.agent_step(signature, skills=[skill], tools=[tool])
        async def classify(text: str, *, agent: ava.Agent) -> str:
            return (await agent(text=text)).verdict

        @ava.workflow
        def flow():
            return classify("clear")

        assert flow().run(executor=LocalExecutor()).result() == "CLEAR"
        build = captured["builds"][0]
        assert build["skills"] == (skill,)
        assert build["tools"] == (tool,)

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
            flow().run(executor=LocalExecutor()).result()

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
            flow().run(executor=LocalExecutor()).result()

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


def test_agent_declaration_metadata_is_complete_static_and_redacted():
    from pathlib import Path

    from predict_rlm import Skill

    def skill_tool(record_id: str) -> str:
        """Fetch a record by identifier."""
        return record_id

    def explicit_tool(text: str) -> str:
        """Normalize text for review."""
        return text.strip()

    audit_skill = Skill(
        name="audit",
        instructions="Check every claim.",
        packages=["pydantic"],
        modules={"audit_helpers": "/private/audit_helpers.py"},
        tools={"fetch_record": skill_tool},
    )

    @ava.agent_step(
        SummarySignature,
        skills=[audit_skill],
        tools=[explicit_tool],
        max_iterations=7,
        output_dir=Path("/private/output"),
        submit_confirmation=lambda _context: "secret callback",
    )
    async def summarize(person: Person, *, agent: ava.Agent) -> str:
        return (await agent(person=person)).note

    spec = summarize.fn.__agent_step__
    metadata = spec.declaration_metadata(
        {
            "max_iterations": 3,
            "debug": True,
            "api_key": "secret",
            "opaque": object(),
        }
    )

    assert metadata["signature"] == {
        "name": "SummarySignature",
        "instructions": "Summarize the supplied person.",
        "inputs": [
            {
                "name": "person",
                "annotation": "Person",
                "description": "person to summarize",
            }
        ],
        "outputs": [
            {
                "name": "summary",
                "annotation": "Summary",
                "description": "structured summary",
            },
            {
                "name": "note",
                "annotation": "str",
                "description": "review note",
            },
        ],
    }
    assert metadata["skills"] == [
        {
            "name": "audit",
            "instructions": "Check every claim.",
            "packages": ["pydantic"],
            "modules": ["audit_helpers"],
            "tools": ["fetch_record"],
        }
    ]
    assert metadata["aggregated_static_instructions"] == (
        "Summarize the supplied person.\n\n## Skill: audit\n\nCheck every claim."
    )
    assert metadata["packages"] == ["pydantic"]
    assert metadata["modules"] == ["audit_helpers"]
    assert metadata["tools"] == [
        {"name": "fetch_record", "description": "Fetch a record by identifier."},
        {"name": "explicit_tool", "description": "Normalize text for review."},
    ]
    assert metadata["runtime"]["max_iterations"] == 7
    assert metadata["runtime"]["debug"] is True
    assert metadata["runtime"]["max_llm_calls"] == 50
    rendered = json.dumps(metadata)
    assert "secret" not in rendered
    assert "/private" not in rendered
    assert "opaque" not in rendered
    assert "submit_confirmation" not in metadata["runtime"]


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


class _RecordingSink:
    strict = True

    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)

    async def flush(self, run_id):
        return None

    async def close(self, run_id, terminal_event=None):
        if terminal_event is not None:
            self.events.append(terminal_event)


class _ExportableTrace:
    def __init__(self, status="completed", *, fail=False):
        self.status = status
        self.fail = fail
        self.called = False

    def to_exportable_json(self):
        self.called = True
        if self.fail:
            raise ValueError("trace export failed")
        return json.dumps(
            {
                "status": self.status,
                "model": "main",
                "sub_model": "sub",
                "iterations": 1,
                "max_iterations": 2,
                "duration_ms": 10,
                "usage": {"main": {}, "sub": {}},
                "steps": [],
                "evidence": {
                    "run_id": "rlm-run",
                    "complete": True,
                    "terminal_outcome": self.status,
                    "events": [],
                },
                "image": "data:image/png;base64,<IMAGE_BASE_64_ENCODED(12)>",
            }
        )


class _EvidencePredictor:
    def __init__(self, sinks, response=None, error=None):
        self.sinks = sinks
        self.response = response
        self.error = error

    async def acall(self, **inputs):
        from predict_rlm import RunEvent, RunEventKind

        payloads = [
            (RunEventKind.RUN_STARTED, {"inputs": inputs}),
            (RunEventKind.CODE_GENERATED, {"iteration": 1, "code": "print('ok')"}),
            (RunEventKind.CODE_EXECUTED, {"iteration": 1, "output": "ok"}),
            (
                RunEventKind.PREDICT_STARTED,
                {
                    "call_id": "predict-1",
                    "signature": "text -> answer",
                    "instructions": "answer",
                    "model": "sub",
                    "input": {"secret": "hidden"},
                },
            ),
            (
                RunEventKind.PREDICT_FINISHED,
                {"call_id": "predict-1", "output": {"secret": "hidden"}},
            ),
            (
                RunEventKind.TOOL_STARTED,
                {"call_id": "tool-1", "name": "lookup", "args": ["secret"]},
            ),
            (
                RunEventKind.TOOL_FINISHED,
                {"call_id": "tool-1", "name": "lookup", "result": "secret"},
            ),
            (
                RunEventKind.ITERATION_RECORDED,
                {
                    "step": {
                        "iteration": 1,
                        "duration_ms": 9,
                        "error": False,
                        "tool_calls": [{}],
                        "predict_calls": [{"calls": [{}]}],
                    }
                },
            ),
        ]
        for sequence, (kind, data) in enumerate(payloads, 1):
            event = RunEvent("rlm-run", sequence, kind, sequence, data)
            for sink in self.sinks:
                await sink.emit(event)
        terminal_kind = (
            RunEventKind.RUN_FAILED if self.error is not None else RunEventKind.RUN_SUCCEEDED
        )
        terminal = RunEvent(
            "rlm-run",
            len(payloads) + 1,
            terminal_kind,
            len(payloads) + 1,
            {"error": str(self.error)} if self.error is not None else {},
        )
        for sink in self.sinks:
            await sink.flush("rlm-run")
            await sink.close("rlm-run", terminal)
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.asyncio
async def test_agent_streams_sanitized_evidence_and_exported_trace(monkeypatch):
    """The observer retains user sinks and publishes only compact live fields."""
    user_sink = _RecordingSink()
    trace = _ExportableTrace()
    prediction = SimpleNamespace(
        summary=Summary(headline="done", person_count=1),
        note="unchanged",
        trace=trace,
    )

    def fake_build(signature, *, skills, tools, **runtime_kwargs):
        sinks = (*runtime_kwargs["events"], agent_step_module._AvalancheEvidenceSink())
        return _EvidencePredictor(sinks, response=prediction)

    monkeypatch.setattr(agent_step_module, "_build_predictor", fake_build)
    agent = agent_step_module.Agent(
        signature=SummarySignature,
        step_name="summarize",
        runtime_kwargs={"events": (user_sink,)},
    )
    observed = []
    with capture_agent_evidence(observed.append):
        result = await agent(person=Person(id=1, name="Ada"))

    assert result is prediction
    assert [event.kind.value for event in user_sink.events] == [
        "run.started",
        "code.generated",
        "code.executed",
        "predict.started",
        "predict.finished",
        "tool.started",
        "tool.finished",
        "iteration.recorded",
        "run.succeeded",
    ]
    live = [event for event in observed if event["kind"] == "evidence"]
    assert [event["sequence"] for event in live] == list(range(1, 10))
    assert live[0]["data"] == {"input_fields": ["person"]}
    assert live[3]["data"]["call_id"] == "predict-1"
    assert "input" not in live[3]["data"]
    assert "output" not in live[4]["data"]
    assert "args" not in live[5]["data"]
    assert "result" not in live[6]["data"]
    terminal = observed[-1]
    assert terminal["kind"] == "trace_finished"
    assert terminal["trace"]["evidence"]["complete"] is True
    assert "IMAGE_BASE_64_ENCODED" in json.dumps(terminal)
    assert trace.called is True


@pytest.mark.asyncio
async def test_agent_observer_and_trace_export_failures_do_not_change_prediction(monkeypatch):
    trace = _ExportableTrace(fail=True)
    prediction = SimpleNamespace(
        summary=Summary(headline="done", person_count=1),
        note="unchanged",
        trace=trace,
    )

    monkeypatch.setattr(
        agent_step_module,
        "_build_predictor",
        lambda *args, **kwargs: _EvidencePredictor(
            (agent_step_module._AvalancheEvidenceSink(),), response=prediction
        ),
    )
    agent = agent_step_module.Agent(
        signature=SummarySignature,
        step_name="summarize",
        runtime_kwargs={},
    )

    def broken_observer(_event):
        raise RuntimeError("devtools unavailable")

    with capture_agent_evidence(broken_observer):
        assert await agent(person=Person(id=1, name="Ada")) is prediction


@pytest.mark.asyncio
async def test_agent_exports_exception_trace_before_preserving_wrapped_error(monkeypatch):
    trace = _ExportableTrace(status="error")
    failure = ValueError("provider unavailable")
    failure.trace = trace
    monkeypatch.setattr(
        agent_step_module,
        "_build_predictor",
        lambda *args, **kwargs: _EvidencePredictor(
            (agent_step_module._AvalancheEvidenceSink(),), error=failure
        ),
    )
    agent = agent_step_module.Agent(
        signature=SummarySignature,
        step_name="summarize",
        runtime_kwargs={},
    )
    observed = []
    with capture_agent_evidence(observed.append):
        with pytest.raises(AgentStepExecutionError, match="provider unavailable"):
            await agent(person=Person(id=1, name="Ada"))

    assert observed[-1]["kind"] == "trace_finished"
    assert observed[-1]["trace"]["status"] == "error"
