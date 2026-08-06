"""Tests for bodyful ``@ava.agent_step`` workflow nodes."""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

import avalanche as ava
from avalanche._agent_evidence import emit_agent_evidence
from avalanche.agent import (
    AgentStepError,
    AgentStepExecutionError,
    capture_agent_evidence,
)
from runtime.executor import LocalExecutor

agent_step_module = importlib.import_module("avalanche.agent.agent_step")


def test_agent_evidence_contracts_are_public_and_typed():
    from avalanche.agent import (
        AgentEvidenceEvent,
        AgentEvidenceListener,
        AgentEvidenceObserverEvent,
        AgentInvocationId,
        AgentTraceFinishedEvent,
        AgentTraceUnavailableEvent,
        ListenerErrorPolicy,
    )
    from avalanche.agent.evidence import capture_agent_evidence as module_capture

    assert module_capture is capture_agent_evidence
    assert AgentEvidenceEvent.__required_keys__ == {
        "kind",
        "invocation_id",
        "sequence",
        "event_kind",
        "timestamp_ns",
        "data",
    }
    assert AgentTraceFinishedEvent.__required_keys__ == {
        "kind",
        "invocation_id",
        "trace",
    }
    assert AgentTraceUnavailableEvent.__required_keys__ == {
        "kind",
        "invocation_id",
        "error",
    }
    assert AgentEvidenceListener is not None
    assert AgentEvidenceObserverEvent is not None
    assert AgentInvocationId is str
    assert ListenerErrorPolicy is not None


def test_agent_evidence_observer_nesting_and_error_policy():
    outer = []
    inner = []

    with capture_agent_evidence(outer.append):
        emit_agent_evidence(
            {
                "kind": "trace_unavailable",
                "invocation_id": "outer-before",
                "error": "before",
            }
        )
        with capture_agent_evidence(inner.append):
            emit_agent_evidence(
                {
                    "kind": "trace_unavailable",
                    "invocation_id": "inner",
                    "error": "inside",
                }
            )
        emit_agent_evidence(
            {
                "kind": "trace_unavailable",
                "invocation_id": "outer-after",
                "error": "after",
            }
        )

    assert [event["error"] for event in outer] == ["before", "after"]
    assert [event["error"] for event in inner] == ["inside"]

    def broken_listener(_event):
        raise RuntimeError("persistence failed")

    with capture_agent_evidence(broken_listener, errors="ignore"):
        emit_agent_evidence(
            {
                "kind": "trace_unavailable",
                "invocation_id": "ignored",
                "error": "ignored",
            }
        )

    with capture_agent_evidence(broken_listener, errors="raise"):
        with pytest.raises(RuntimeError, match="persistence failed"):
            emit_agent_evidence(
                {
                    "kind": "trace_unavailable",
                    "invocation_id": "raised",
                    "error": "raised",
                }
            )

    def interrupted_listener(_event):
        raise KeyboardInterrupt

    with capture_agent_evidence(interrupted_listener, errors="ignore"):
        with pytest.raises(KeyboardInterrupt):
            emit_agent_evidence(
                {
                    "kind": "trace_unavailable",
                    "invocation_id": "interrupted",
                    "error": "interrupted",
                }
            )

    with pytest.raises(ValueError, match="errors must be 'raise' or 'ignore'"):
        with capture_agent_evidence(outer.append, errors="invalid"):
            pass


@pytest.mark.asyncio
async def test_agent_evidence_observers_are_isolated_across_async_contexts():
    ready = asyncio.Event()
    arrivals = 0

    async def observe(name):
        nonlocal arrivals
        events = []
        with capture_agent_evidence(events.append, errors="raise"):
            arrivals += 1
            if arrivals == 2:
                ready.set()
            await ready.wait()
            await asyncio.sleep(0)
            emit_agent_evidence(
                {
                    "kind": "trace_unavailable",
                    "invocation_id": name,
                    "error": name,
                }
            )
        return events

    left, right = await asyncio.gather(observe("left"), observe("right"))

    assert [event["error"] for event in left] == ["left"]
    assert [event["error"] for event in right] == ["right"]


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


def test_build_predictor_uses_declared_predict_rlm_event_contract():
    predictor = agent_step_module._build_predictor(
        "question -> answer",
        skills=(),
        tools=(),
    )

    assert len(predictor.runtime_spec.events) == 1
    assert isinstance(
        predictor.runtime_spec.events[0], agent_step_module._AvalancheEvidenceSink
    )


@pytest.mark.asyncio
async def test_predict_rlm_recorder_honors_observer_error_policy():
    """The real recorder must not swallow a strict bridge persistence failure."""
    from predict_rlm import (
        EvidenceIncompleteError,
        EvidenceRecorder,
        RunContext,
        RunEventKind,
    )

    class AnswerSignature(ava.Signature):
        question: str = ava.InputField()
        answer: str = ava.OutputField()

    predictor = agent_step_module._build_predictor(
        AnswerSignature,
        skills=(),
        tools=(),
    )

    class RecorderPredictor:
        async def acall(self, **inputs):
            recorder = EvidenceRecorder(
                RunContext(predictor.runtime_spec, inputs),
                predictor.runtime_spec.events,
            )
            await recorder.emit(RunEventKind.RUN_STARTED, inputs=inputs)
            return SimpleNamespace(answer="ok")

    class ListenerPersistenceError(RuntimeError):
        pass

    def broken_incremental_listener(event):
        if event["kind"] == "evidence":
            raise ListenerPersistenceError("persistence failed")

    ignored_agent = agent_step_module.Agent(
        signature=AnswerSignature,
        step_name="answer",
        runtime_kwargs={},
    )
    ignored_agent._predictor = RecorderPredictor()
    with capture_agent_evidence(broken_incremental_listener, errors="ignore"):
        prediction = await ignored_agent(question="ignored")
    assert prediction.answer == "ok"

    strict_agent = agent_step_module.Agent(
        signature=AnswerSignature,
        step_name="answer",
        runtime_kwargs={},
    )
    strict_agent._predictor = RecorderPredictor()
    with capture_agent_evidence(broken_incremental_listener, errors="raise"):
        with pytest.raises(AgentStepExecutionError) as raised:
            await strict_agent(question="strict")

    recorder_error = raised.value.__cause__
    assert isinstance(recorder_error, EvidenceIncompleteError)
    assert isinstance(recorder_error.__cause__, ListenerPersistenceError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listener_failure",
    [
        KeyboardInterrupt("listener interrupted"),
        SystemExit("listener requested exit"),
    ],
)
async def test_predict_rlm_recorder_reraises_exact_listener_base_exception(
    listener_failure,
):
    """PredictRLM wrapping must not replace a listener control-flow exception."""
    from predict_rlm import EvidenceRecorder, RunContext, RunEventKind

    class AnswerSignature(ava.Signature):
        question: str = ava.InputField()
        answer: str = ava.OutputField()

    predictor = agent_step_module._build_predictor(
        AnswerSignature,
        skills=(),
        tools=(),
    )

    class RecorderPredictor:
        async def acall(self, **inputs):
            recorder = EvidenceRecorder(
                RunContext(predictor.runtime_spec, inputs),
                predictor.runtime_spec.events,
            )
            await recorder.emit(RunEventKind.RUN_STARTED, inputs=inputs)
            return SimpleNamespace(answer="ok")

    def interrupted_incremental_listener(event):
        if event["kind"] == "evidence":
            raise listener_failure

    agent = agent_step_module.Agent(
        signature=AnswerSignature,
        step_name="answer",
        runtime_kwargs={},
    )
    agent._predictor = RecorderPredictor()

    with capture_agent_evidence(interrupted_incremental_listener, errors="raise"):
        with pytest.raises(type(listener_failure)) as raised:
            await agent(question="strict")

    assert raised.value is listener_failure
    assert not isinstance(raised.value, AgentStepExecutionError)


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
        assert captured["builds"][0]["runtime_kwargs"] == {"lm": "root-lm", "verbose": False}

    def test_agent_step_runtime_kwargs_override_quiet_default(self, monkeypatch):
        """An explicit step setting takes precedence over the quiet agent default."""
        captured = install_fake(
            monkeypatch,
            lambda _inputs: SimpleNamespace(
                summary=Summary(headline="about Ada", person_count=1),
                note="ready",
            ),
        )

        @ava.agent_step(SummarySignature, verbose=True)
        async def summarize(person: Person, *, agent: ava.Agent) -> str:
            return (await agent(person=person)).note

        @ava.workflow
        def flow():
            return summarize(Person(id=1, name="Ada"))

        assert flow().run(executor=LocalExecutor()).result() == "ready"
        assert captured["builds"][0]["runtime_kwargs"]["verbose"] is True

    def test_workflow_agent_defaults_override_quiet_default(self, monkeypatch):
        """A workflow may opt all of its agent steps into verbose traces."""
        captured = install_fake(
            monkeypatch,
            lambda _inputs: SimpleNamespace(
                summary=Summary(headline="about Ada", person_count=1),
                note="ready",
            ),
        )

        @ava.agent_step(SummarySignature)
        async def summarize(person: Person, *, agent: ava.Agent) -> str:
            return (await agent(person=person)).note

        @ava.workflow(agent_defaults={"verbose": True})
        def flow():
            return summarize(Person(id=1, name="Ada"))

        assert flow().run(executor=LocalExecutor()).result() == "ready"
        assert captured["builds"][0]["runtime_kwargs"]["verbose"] is True

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
            "lm": "workflow-main",
            "sub_lm": "workflow-sub",
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
    assert metadata["runtime"]["verbose"] is False
    rendered = json.dumps(metadata)
    assert "secret" not in rendered
    assert metadata["models"] == {
        "main": {"source": "workflow default", "identity": "workflow-main"},
        "sub": {"source": "workflow default", "identity": "workflow-sub"},
    }
    assert spec.declaration_metadata()["models"] == {
        "main": {"source": "PredictRLM default"},
        "sub": {"source": "PredictRLM default"},
    }
    assert "/private" not in rendered
    assert "opaque" not in rendered
    assert "submit_confirmation" not in metadata["runtime"]


@pytest.mark.parametrize("runtime_key", ("lm", "sub_lm"))
def test_agent_declaration_metadata_describes_custom_model_types(runtime_key):
    class CustomModel:
        pass

    @ava.agent_step(SummarySignature)
    async def summarize(person: Person, *, agent: ava.Agent) -> str:
        return (await agent(person=person)).note

    metadata = summarize.fn.__agent_step__.declaration_metadata({runtime_key: CustomModel()})
    label = "main" if runtime_key == "lm" else "sub"
    identity = metadata["models"][label]["identity"]
    assert identity["type"].endswith(".CustomModel")


@pytest.mark.parametrize(
    ("runtime_key", "descriptor", "expected_type"),
    [
        ("lm", {"client": object()}, "builtins.object"),
        ("sub_lm", ["supported", object()], "builtins.object"),
    ],
)
def test_agent_declaration_metadata_describes_nested_custom_model_types(
    runtime_key, descriptor, expected_type
):
    @ava.agent_step(SummarySignature)
    async def summarize(person: Person, *, agent: ava.Agent) -> str:
        return (await agent(person=person)).note

    metadata = summarize.fn.__agent_step__.declaration_metadata({runtime_key: descriptor})
    label = "main" if runtime_key == "lm" else "sub"
    identity = metadata["models"][label]["identity"]
    nested = identity["client"] if isinstance(identity, dict) else identity[1]
    assert nested == {"type": expected_type}


def test_agent_declaration_metadata_preserves_supported_nested_model_descriptors():
    @ava.agent_step(SummarySignature)
    async def summarize(person: Person, *, agent: ava.Agent) -> str:
        return (await agent(person=person)).note

    metadata = summarize.fn.__agent_step__.declaration_metadata(
        {
            "lm": {
                "provider": "openai",
                "options": {"model": "gpt-5", "temperature": 0.2},
                "api_key": "secret",
            },
            "sub_lm": ["anthropic", {"model": "claude"}],
        }
    )

    assert metadata["models"] == {
        "main": {
            "source": "workflow default",
            "identity": {
                "provider": "openai",
                "options": {"model": "gpt-5", "temperature": 0.2},
            },
        },
        "sub": {
            "source": "workflow default",
            "identity": ["anthropic", {"model": "claude"}],
        },
    }
    assert "secret" not in json.dumps(metadata)


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
                        "reasoning": "Check the evidence before summarizing.",
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
            {"error": str(self.error)}
            if self.error is not None
            else {"status": "completed", "outputs": {"summary": "done"}},
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
    assert live[0]["data"] == {
        "input_fields": ["person"],
        "inputs": {"person": {"id": 1, "name": "Ada"}},
    }
    assert live[3]["data"]["call_id"] == "predict-1"
    assert "input" not in live[3]["data"]
    assert "output" not in live[4]["data"]
    assert "args" not in live[5]["data"]
    assert live[7]["data"]["step"]["reasoning"] == "Check the evidence before summarizing."
    assert live[8]["data"] == {"status": "completed", "outputs": {"summary": "done"}}
    assert "result" not in live[6]["data"]
    terminal = observed[-1]
    assert terminal["kind"] == "trace_finished"
    assert terminal["trace"]["evidence"]["complete"] is True
    assert "IMAGE_BASE_64_ENCODED" in json.dumps(terminal)
    assert trace.called is True


@pytest.mark.asyncio
async def test_successful_terminal_listener_failure_is_not_reclassified():
    prediction = SimpleNamespace(trace=_ExportableTrace())

    class SuccessfulPredictor:
        async def acall(self, **inputs):
            return prediction

    agent = agent_step_module.Agent(
        signature=SummarySignature,
        step_name="summarize",
        runtime_kwargs={},
    )
    agent._predictor = SuccessfulPredictor()
    observed = []
    listener_error = RuntimeError("terminal persistence failed")

    def persist_then_fail(event):
        observed.append(event)
        if event["kind"] == "trace_finished":
            raise listener_error

    with capture_agent_evidence(persist_then_fail, errors="raise"):
        with pytest.raises(RuntimeError) as raised:
            await agent(person=Person(id=1, name="Ada"))

    assert raised.value is listener_error
    terminal_events = [
        event for event in observed if event["kind"] in {"trace_finished", "trace_unavailable"}
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0]["kind"] == "trace_finished"


@pytest.mark.asyncio
async def test_concurrent_agent_calls_have_stable_generated_invocation_ids(monkeypatch):
    """One outer observer can deterministically correlate interleaved calls."""
    from predict_rlm import RunEvent, RunEventKind

    generated_ids = iter(("generated-left", "generated-right"))
    monkeypatch.setattr(
        agent_step_module.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=next(generated_ids)),
    )

    class CorrelationSignature(ava.Signature):
        request_id: str = ava.InputField()
        answer: str = ava.OutputField()

    class CorrelatedTrace:
        def __init__(self, request_id):
            self.request_id = request_id

        def to_exportable_json(self):
            return json.dumps(
                {
                    "status": "completed",
                    "evidence": {
                        "run_id": self.request_id,
                        "complete": True,
                        "terminal_outcome": "completed",
                        "events": [],
                    },
                    "steps": [],
                }
            )

    class ConcurrentPredictor:
        def __init__(self):
            self.sink = agent_step_module._AvalancheEvidenceSink()
            self.ready = asyncio.Event()
            self.arrivals = 0

        async def acall(self, **inputs):
            request_id = inputs["request_id"]
            await self.sink.emit(
                RunEvent(
                    request_id,
                    1,
                    RunEventKind.PREDICT_STARTED,
                    1,
                    {
                        "call_id": request_id,
                        "invocation_id": f"caller-{request_id}",
                    },
                )
            )
            self.arrivals += 1
            if self.arrivals == 2:
                self.ready.set()
            await self.ready.wait()
            await asyncio.sleep(0)
            await self.sink.close(
                request_id,
                RunEvent(
                    request_id,
                    2,
                    RunEventKind.RUN_SUCCEEDED,
                    2,
                    {},
                ),
            )
            return SimpleNamespace(
                answer=request_id,
                trace=CorrelatedTrace(request_id),
            )

    agent = agent_step_module.Agent(
        signature=CorrelationSignature,
        step_name="correlate",
        runtime_kwargs={},
    )
    agent._predictor = ConcurrentPredictor()
    observed = []

    with capture_agent_evidence(observed.append, errors="raise"):
        left, right = await asyncio.gather(
            agent(request_id="left"),
            agent(request_id="right"),
        )

    assert (left.answer, right.answer) == ("left", "right")
    grouped = {}
    for event in observed:
        grouped.setdefault(event["invocation_id"], []).append(event)

    assert set(grouped) == {"generated-left", "generated-right"}
    assert not set(grouped) & {"left", "right", "caller-left", "caller-right"}
    correlations = {}
    for invocation_id, events in grouped.items():
        assert [event["kind"] for event in events] == [
            "evidence",
            "evidence",
            "trace_finished",
        ]
        call_id = events[0]["data"]["call_id"]
        assert events[0]["data"].get("invocation_id") is None
        assert events[-1]["trace"]["evidence"]["run_id"] == call_id
        correlations[call_id] = invocation_id
    assert set(correlations) == {"left", "right"}


@pytest.mark.asyncio
@pytest.mark.parametrize("exportable_trace", [False, True])
async def test_agent_cancellation_emits_one_typed_terminal_and_reraises_exactly(
    exportable_trace,
):
    cancellation = asyncio.CancelledError("cancelled by caller")
    if exportable_trace:
        cancellation.trace = _ExportableTrace(status="cancelled")

    class CancelledPredictor:
        async def acall(self, **inputs):
            raise cancellation

    agent = agent_step_module.Agent(
        signature=SummarySignature,
        step_name="summarize",
        runtime_kwargs={},
    )
    agent._predictor = CancelledPredictor()
    observed = []

    with capture_agent_evidence(observed.append, errors="raise"):
        with pytest.raises(asyncio.CancelledError) as raised:
            await agent(person=Person(id=1, name="Ada"))

    assert raised.value is cancellation
    assert len(observed) == 1
    assert observed[0]["invocation_id"]
    if exportable_trace:
        assert observed[0]["kind"] == "trace_finished"
        assert observed[0]["trace"]["status"] == "cancelled"
    else:
        assert observed[0] == {
            "kind": "trace_unavailable",
            "invocation_id": observed[0]["invocation_id"],
            "error": "cancelled by caller",
        }


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


def test_agent_value_projection_preserves_nested_predict_rlm_files():
    from predict_rlm import File

    projected = agent_step_module._bounded_agent_value(
        {
            "source": File(path="/tmp/source.pdf"),
            "outputs": [File(path="/tmp/report.xlsx")],
        }
    )

    assert projected == {
        "source": {"kind": "predict_rlm_file", "path": "/tmp/source.pdf"},
        "outputs": [{"kind": "predict_rlm_file", "path": "/tmp/report.xlsx"}],
    }


def test_agent_value_projection_marks_unsupported_and_oversized_values_unavailable():
    unsupported = agent_step_module._bounded_agent_value({"value": object()})
    oversized = agent_step_module._bounded_agent_value(
        "x" * (agent_step_module._MAX_EVIDENCE_VALUE_BYTES + 1)
    )

    assert unsupported == {
        "value": {"kind": "unavailable", "reason": "unsupported value type: object"}
    }
    assert oversized == {"kind": "unavailable", "reason": "value exceeds byte limit"}
