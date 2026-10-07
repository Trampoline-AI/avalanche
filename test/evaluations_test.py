"""Evaluation behavior through the same real SDK HTTP fake as classifier steps."""

from __future__ import annotations

import copy
import json
import traceback
from collections import UserDict
from datetime import date
from pathlib import Path

import httpx2
import pytest
import typesafe_sdk
from pydantic import BaseModel, ValidationError

from avalanche.evaluations import (
    EvalContext,
    EvaluationDeclaration,
    EvaluationError,
    Evaluations,
    Metric,
    MetricInput,
)

pytest_plugins = ["classifier_test"]


def noul(state):
    return Metric(state=state, question={"type": "noul", "instructions": "Is it supported?"})


@pytest.fixture
def noul_service(service):
    async def respond(request):
        payload = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "model": "jev-test-resolved",
                "answers": {
                    name: {"type": "noul", "noul": 0.8} for name in payload["questions"]
                },
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        )

    service.handler = respond
    return service


@pytest.mark.asyncio
async def test_shared_states_batch_without_combining_different_evidence(noul_service):
    evaluations = Evaluations(
        metrics={
            "summary": noul(lambda ctx: {"text": ctx.output, "flag": True}),
            "same": noul(lambda ctx: {"flag": True, "text": ctx.output}),
            "numeric": noul(lambda ctx: {"text": ctx.output, "flag": 1}),
            "source": noul(lambda ctx: ctx.inputs["source"]),
        }
    )
    result = await evaluations.evaluate(
        EvalContext(inputs={"source": "independent source"}, output="summary text")
    )
    requests = [json.loads(request.content) for request in noul_service.requests]
    assert [(item["state"], set(item["questions"])) for item in requests] == [
        ({"text": "summary text", "flag": True}, {"summary", "same"}),
        ({"text": "summary text", "flag": 1}, {"numeric"}),
        ("independent source", {"source"}),
    ]
    assert list(result.classification.answers) == ["summary", "same", "numeric", "source"]
    assert result.classification.usage.input_tokens == 30
    assert result.classification.usage.output_tokens == 6
    assert all(transport.closed for transport in noul_service.transports)


@pytest.mark.asyncio
async def test_existing_objects_support_field_combination_and_all_traces(noul_service):
    class Report(BaseModel):
        summary: str
        conclusion: str
        unrelated_private_field: str

    trace = [
        {"kind": "agent_trace_finished", "invocation_id": "first", "trace": {"steps": []}},
        {
            "kind": "agent_trace_unavailable",
            "invocation_id": "second",
            "error": "trace exporter unavailable",
        },
    ]
    context = EvalContext(
        inputs={"topic": "testing", "opaque": object()},
        output=Report(
            summary="short summary",
            conclusion="check boundaries",
            unrelated_private_field="secret",
        ),
        trace=trace,
    )
    evaluations = Evaluations(
        metrics={
            "summary": noul(lambda ctx: ctx.output.summary),
            "relevance": noul(
                lambda ctx: {"topic": ctx.inputs["topic"], "answer": ctx.output.conclusion}
            ),
            "trace": noul(lambda ctx: ctx.trace),
        }
    )
    await evaluations.evaluate(context)
    assert [json.loads(request.content)["state"] for request in noul_service.requests] == [
        "short summary",
        {"topic": "testing", "answer": "check boundaries"},
        trace,
    ]


@pytest.mark.asyncio
async def test_questions_are_snapshotted_and_all_answer_metadata_survives(
    questions, response_body, service
):
    original = copy.deepcopy(questions)
    metrics = {
        name: Metric(state=lambda ctx: ctx.output, question=question)
        for name, question in questions.items()
    }
    evaluations = Evaluations(metrics=metrics)
    questions["department"]["criteria"]["billing"]["examples"].clear()
    metrics["severity"].question["criteria"].clear()
    metrics.clear()
    result = await evaluations.evaluate(EvalContext(inputs={}, output="charged twice"))
    assert result.classification.model_dump(mode="json") == response_body
    assert result.classification.choices["department"].choice == "billing"
    assert result.classification.nouls["urgent"].noul == 0.91
    assert result.classification.scores["severity"].legend == {
        "0": "minor",
        "1": {"impact": ["service unavailable"]},
    }
    sent_questions = json.loads(service.requests[0].content)["questions"]
    assert sent_questions["department"]["criteria"] == original["department"]["criteria"]
    assert sent_questions["severity"]["criteria"] == original["severity"]["criteria"]


@pytest.mark.asyncio
async def test_composites_explicitly_normalize_scores_and_preserve_raw_answers(service):
    service.response_body = {
        "model": "jev-test-resolved",
        "answers": {
            "clarity": {
                "type": "score",
                "score": 1.5,
                "legend": {"0": "Confusing", "1": "Mostly clear", "2": "Clear"},
                "probabilities": {"0": 0.0, "1": 0.5, "2": 0.5},
                "confidence": 0.4,
            },
            "checked_sources": {"type": "noul", "noul": 0.8},
        },
        "usage": {"input_tokens": 23, "output_tokens": 4},
    }
    evaluations = Evaluations(
        metrics={
            "clarity": Metric(
                state=lambda ctx: ctx.output,
                question={
                    "type": "score",
                    "instructions": "How clear is the answer?",
                    "criteria": ["Confusing", "Mostly clear", "Clear"],
                },
            ),
            "checked_sources": noul(lambda ctx: ctx.output),
        },
        composites={
            "quality": lambda answers: (
                0.7 * answers.scores["clarity"].score / 2
                + 0.3 * answers.nouls["checked_sources"].noul
            )
        },
    )
    result = await evaluations.evaluate(EvalContext(inputs={}, output="summary"))
    assert result.composites["quality"] == pytest.approx(0.765)
    assert result.classification.model_dump(mode="json") == service.response_body
    assert len(service.requests) == 1


@pytest.mark.asyncio
async def test_composites_cannot_overwrite_raw_answers_or_each_others_inputs(noul_service):
    def mutate(answers):
        answers.answers.clear()
        return 0

    result = await Evaluations(
        metrics={"fact": noul(lambda ctx: ctx.output)},
        composites={"mutating": mutate, "original": lambda answers: answers.nouls["fact"].noul},
    ).evaluate(EvalContext(inputs={}, output="fact"))
    assert result.classification.nouls["fact"].noul == 0.8
    assert result.composites == {"mutating": 0.0, "original": 0.8}


@pytest.mark.asyncio
async def test_unknown_usage_remains_unknown_per_counter(noul_service):
    async def respond(request):
        payload = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "model": "jev-test-resolved",
                "answers": {
                    name: {"type": "noul", "noul": 0.8} for name in payload["questions"]
                },
                "usage": {"output_tokens": 2}
                if payload["state"] == "second"
                else {"input_tokens": 10, "output_tokens": 2},
            },
        )

    noul_service.handler = respond
    result = await Evaluations(
        metrics={"one": noul(lambda ctx: "first"), "two": noul(lambda ctx: "second")}
    ).evaluate(EvalContext(inputs={}, output=None))
    assert result.classification.usage.input_tokens is None
    assert result.classification.usage.output_tokens == 4


@pytest.mark.asyncio
async def test_evaluation_overrides_workflow_runtime_without_leaking(noul_service, monkeypatch):
    client = typesafe_sdk.AsyncTypeSafeClient
    settings = []

    def configured_client(**kwargs):
        settings.append(kwargs)
        return client(**kwargs)

    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", configured_client)
    evaluations = Evaluations(metrics={"fact": noul(lambda ctx: ctx.output)}, timeout=3)
    context = EvalContext(inputs={}, output="fact")
    await evaluations.evaluate(
        context, runtime_defaults={"model": "workflow-model", "timeout": 9}
    )
    await evaluations.evaluate(context)
    assert settings == [
        {"model": "workflow-model", "timeout": 3.0},
        {"model": "jev-latest", "timeout": 3.0},
    ]


@pytest.mark.parametrize(
    "question",
    [
        {"type": "bool", "instructions": "Question"},
        {"type": "score", "criteria": ["one"]},
        {"type": "choice", "criteria": {}},
        {"type": "noul", "criteria": {"yes": "yes"}},
        {"type": "noul", "instructions": {"invalid": float("nan")}},
        {"type": "noul", "extra": "not a classifier field"},
    ],
)
def test_invalid_questions_fail_at_declaration_without_network(question, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    def forbidden_client(**kwargs):
        pytest.fail("declaration must not create a client")

    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", forbidden_client)
    with pytest.raises(EvaluationError, match="metric question"):
        Metric(state=lambda ctx: ctx.output, question=question)


@pytest.mark.parametrize(
    "declaration",
    [
        {"metrics": {}},
        {"metrics": []},
        {"metrics": {1: noul(lambda ctx: ctx.output)}},
        {"metrics": {"": noul(lambda ctx: ctx.output)}},
        {"metrics": {"bad": lambda ctx: ctx.output}},
        {"metrics": {"ok": noul(lambda ctx: ctx.output)}, "model": " "},
        {"metrics": {"ok": noul(lambda ctx: ctx.output)}, "model": 42},
        {"metrics": {"ok": noul(lambda ctx: ctx.output)}, "timeout": 0},
        {"metrics": {"ok": noul(lambda ctx: ctx.output)}, "timeout": "3"},
        {"metrics": {"ok": noul(lambda ctx: ctx.output)}, "timeout": True},
        {"metrics": {"ok": noul(lambda ctx: ctx.output)}, "timeout": float("nan")},
        {"metrics": {"ok": noul(lambda ctx: ctx.output)}, "timeout": float("inf")},
        {"metrics": {"ok": noul(lambda ctx: ctx.output)}, "composites": {"bad": 1}},
        {"metrics": {"ok": noul(lambda ctx: ctx.output)}, "composites": []},
        {"metrics": {"ok": noul(lambda ctx: ctx.output)}, "composites": {"": lambda r: 0.5}},
        {"metrics": {"ok": noul(lambda ctx: ctx.output)}, "unknown": "setting"},
    ],
)
def test_invalid_evaluation_declarations_fail_early(declaration):
    with pytest.raises(ValidationError):
        Evaluations(**declaration)


def test_discovery_requires_neither_credentials_nor_context_values(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("metadata must not select state, run composites, or construct a client")

    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", forbidden)
    evaluations = Evaluations(
        metrics={"opaque": noul(forbidden)},
        composites={"score": forbidden},
        model="discovery-model",
    )
    declaration = evaluations.declaration_metadata({"model": "workflow-model", "timeout": 8})
    assert declaration.metrics["opaque"].instructions == "Is it supported?"
    assert declaration.composites == ("score",)
    assert declaration.runtime.model == "discovery-model"
    assert declaration.runtime.timeout == 8
    assert declaration.metric_inputs == {
        "opaque": (MetricInput(source="custom", selector=forbidden.__qualname__),)
    }


def test_declaration_runtime_precedence_does_not_mutate_workflow_defaults():
    defaults = {"model": "workflow-model", "timeout": 8}
    evaluations = Evaluations(metrics={"fact": noul(lambda ctx: ctx.output)}, timeout=3)
    inherited = evaluations.declaration_metadata(defaults)
    assert inherited.runtime.model == "workflow-model"
    assert inherited.runtime.timeout == 3
    assert defaults == {"model": "workflow-model", "timeout": 8}
    independent = evaluations.declaration_metadata()
    assert independent.runtime.model == "jev-latest"
    assert independent.runtime.timeout == 3
    assert (
        Evaluations(metrics={"fact": noul(lambda ctx: ctx.output)})
        .declaration_metadata()
        .runtime.timeout
        == 10
    )


def test_declaration_metadata_owns_nested_questions_and_public_collections():
    question = {
        "type": "choice",
        "instructions": {"ask": ["Choose the supported conclusion"]},
        "criteria": {"supported": {"evidence": ["cited"]}, "unsupported": None},
    }
    metrics = UserDict({"grounded": Metric(state=lambda ctx: ctx.output, question=question)})
    composites = UserDict({"score": lambda answers: 0.5})
    evaluations = Evaluations[str, str](metrics=metrics, composites=composites)
    original = evaluations.declaration_metadata().model_dump_json()
    question["instructions"]["ask"].clear()
    metrics.clear()
    composites.clear()
    with pytest.raises(TypeError):
        evaluations.metrics["injected"] = noul(lambda ctx: ctx.output)
    with pytest.raises(TypeError):
        evaluations.composites["score"] = lambda answers: 1.0
    with pytest.raises(ValidationError):
        evaluations.metrics = {}
    exported = evaluations.declaration_metadata()
    exported.metrics["grounded"].instructions["ask"].append("mutated metadata")
    exported.metrics["grounded"].criteria.clear()
    exported.metrics.clear()
    exported.metric_inputs.clear()
    retained = EvaluationDeclaration.model_validate_json(
        evaluations.declaration_metadata().model_dump_json()
    )
    assert retained.model_dump_json() == original
    assert retained.composites == ("score",)
    assert retained.metrics["grounded"].instructions == {
        "ask": ["Choose the supported conclusion"]
    }


def test_adjacent_lambdas_describe_only_their_own_context_reads():
    first, second = lambda ctx: ctx.output.summary, lambda ctx: ctx.inputs["packet"]
    evaluations = Evaluations(
        metrics={
            "first": noul(first),
            "second": noul(second),
            "combined": noul(
                lambda ctx: {
                    "fake_source_path": ctx.output.customer_update_draft,
                    "summary_again": ctx.output.summary,
                    "duplicate": ctx.output.summary,
                    "events": ctx.trace,
                }
            ),
        }
    )
    assert evaluations.declaration_metadata().metric_inputs == {
        "first": (MetricInput(source="output", selector="summary"),),
        "second": (MetricInput(source="input", selector="['packet']"),),
        "combined": (
            MetricInput(source="output", selector="customer_update_draft"),
            MetricInput(source="output", selector="summary"),
            MetricInput(source="trace", selector=""),
        ),
    }


def test_named_helpers_describe_explicit_reads_not_dict_keys_or_methods():
    def grounded(ctx):
        return {
            "source": ctx.inputs["packet"].model_dump(mode="json"),
            "handoff": ctx.output.model_dump(mode="json"),
        }

    def inspected(ctx):
        actions = [event["output"] for event in ctx.trace]
        return {"actions": actions, "source": ctx.inputs["packet"].model_dump(mode="json")}

    def nested(ctx):
        return ctx.output.items[0].text, ctx.inputs["packet"].facts["observed"]

    declaration = Evaluations(
        metrics={
            "grounded": noul(grounded),
            "inspected": noul(inspected),
            "nested": noul(nested),
        }
    ).declaration_metadata()
    assert declaration.metric_inputs == {
        "grounded": (
            MetricInput(source="input", selector="['packet']"),
            MetricInput(source="output", selector=""),
        ),
        "inspected": (
            MetricInput(source="trace", selector=""),
            MetricInput(source="input", selector="['packet']"),
        ),
        "nested": (
            MetricInput(source="output", selector="items[0].text"),
            MetricInput(source="input", selector="['packet'].facts['observed']"),
        ),
    }


def test_opaque_or_delegating_selectors_do_not_invent_context_provenance():
    def helper(ctx):
        return ctx.trace

    def delegated(ctx):
        return helper(ctx)

    class Opaque:
        def __call__(self, ctx):
            return ctx.output

    unavailable = eval("lambda ctx: ctx.output", {"__builtins__": {}})
    declaration = Evaluations(
        metrics={
            "delegated": noul(delegated),
            "object": noul(Opaque()),
            "unavailable": noul(unavailable),
        }
    ).declaration_metadata()
    assert declaration.metric_inputs == {
        "delegated": (MetricInput(source="custom", selector=delegated.__qualname__),),
        "object": (
            MetricInput(source="custom", selector=f"{Opaque.__module__}.{Opaque.__qualname__}"),
        ),
        "unavailable": (MetricInput(source="custom", selector="<lambda>"),),
    }


def test_historic_metadata_defaults_to_no_provenance_and_new_fields_are_strict():
    historic = EvaluationDeclaration.model_validate_json(
        '{"metrics":{"fact":{"type":"noul","instructions":"Is it true?"}},'
        '"composites":[],"runtime":{"model":"jev-latest","timeout":10}}'
    )
    assert historic.metric_inputs == {}
    for invalid in (
        {"source": "payload", "selector": ""},
        {"source": "output", "selector": 1},
        {"source": "trace", "selector": "", "code": "private source"},
    ):
        with pytest.raises(ValidationError):
            MetricInput.model_validate(invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", [None, {"bad": float("nan")}, date(2026, 1, 1), Path("a.txt")]
)
async def test_python_only_or_nonfinite_selected_state_fails_privately(invalid, noul_service):
    evaluations = Evaluations(metrics={"selected": noul(lambda ctx: invalid)})
    with pytest.raises(EvaluationError, match="State validation for metric 'selected'"):
        await evaluations.evaluate(EvalContext(inputs={}, output=object()))
    assert noul_service.requests == []


@pytest.mark.asyncio
async def test_selector_errors_identify_metric_without_exposing_state(noul_service):
    secret = "private-selector-payload"
    evaluations = Evaluations(metrics={"missing": noul(lambda ctx: ctx.inputs[secret])})
    with pytest.raises(
        EvaluationError, match="State selector for metric 'missing'.*KeyError"
    ) as caught:
        await evaluations.evaluate(EvalContext(inputs={}, output=None))
    assert secret not in "".join(traceback.format_exception(caught.value))
    assert noul_service.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [True, "0.5", float("nan"), float("inf"), -0.1, 1.1])
async def test_invalid_composite_results_fail_without_clamping(invalid, noul_service):
    evaluations = Evaluations(
        metrics={"fact": noul(lambda ctx: ctx.output)},
        composites={"invalid": lambda answers: invalid},
    )
    with pytest.raises(EvaluationError, match="Composite 'invalid'.*finite number"):
        await evaluations.evaluate(EvalContext(inputs={}, output="fact"))


@pytest.mark.asyncio
async def test_composite_failure_is_private_and_names_the_composite(noul_service):
    def broken(answers):
        raise ValueError("private-composite-payload")

    with pytest.raises(EvaluationError, match="Composite 'quality'.*ValueError") as caught:
        await Evaluations(
            metrics={"fact": noul(lambda ctx: ctx.output)}, composites={"quality": broken}
        ).evaluate(EvalContext(inputs={}, output="fact"))
    assert "private-composite-payload" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
async def test_malformed_answers_use_existing_classifier_validation_and_close(service):
    service.response_body = {
        "model": "jev-test-resolved",
        "answers": {"fact": {"type": "noul", "noul": 0.8}},
        "usage": {},
    }
    evaluations = Evaluations(
        metrics={
            "fact": Metric(
                state=lambda ctx: ctx.output,
                question={"type": "choice", "criteria": {"yes": None, "no": None}},
            )
        }
    )
    with pytest.raises(
        EvaluationError, match="Jev evaluation for metrics 'fact'.*Invalid TypeSafe response"
    ):
        await evaluations.evaluate(EvalContext(inputs={}, output="state"))
    assert all(transport.closed for transport in service.transports)


@pytest.mark.asyncio
async def test_service_failure_exposes_stage_not_credentials_or_echoed_state(noul_service):
    secret = "private-server-state"

    async def fail(request):
        return httpx2.Response(401, json={"error": "private-server-state test-only-key"})

    noul_service.handler = fail
    with pytest.raises(
        EvaluationError, match="Jev evaluation for metrics 'fact'.*HTTP 401"
    ) as caught:
        await Evaluations(metrics={"fact": noul(lambda ctx: ctx.output)}).evaluate(
            EvalContext(inputs={}, output=secret)
        )
    formatted = "".join(traceback.format_exception(caught.value))
    assert "private-server-state" not in formatted
    assert "test-only-key" not in formatted
    assert all(transport.closed for transport in noul_service.transports)


@pytest.mark.asyncio
async def test_path_strings_are_sent_as_text_without_opening_files(noul_service, tmp_path):
    path = tmp_path / "missing-evidence.txt"
    await Evaluations(metrics={"path": noul(lambda ctx: ctx.output)}).evaluate(
        EvalContext(inputs={}, output=str(path))
    )
    assert json.loads(noul_service.requests[0].content)["state"] == str(path)
    assert not path.exists()
