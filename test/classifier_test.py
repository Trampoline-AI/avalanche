"""Classifier contracts exercised through the real TypeSafe SDK HTTP boundary."""

from __future__ import annotations

import asyncio
import copy
import json
import traceback
from collections import defaultdict

import httpx2
import pytest
import typesafe_sdk

import avalanche as ava
from avalanche.classifier import (
    ClassifierStepError,
    ClassifierStepExecutionError,
    capture_classifier_evidence,
)
from runtime.operator import WorkflowRegistry


@pytest.fixture
def questions():
    return {
        "department": {
            "type": "choice",
            "instructions": {"task": "Route the ticket", "context": ["customer support"]},
            "criteria": {"billing": {"examples": ["duplicate charge"]}, "technical": None},
        },
        "urgent": {
            "type": "noul",
            "instructions": "Does the ticket require action today?",
            "criteria": None,
        },
        "severity": {
            "type": "score",
            "instructions": ["Assess severity", {"consider": "customer impact"}],
            "criteria": ["minor", {"impact": ["service unavailable"]}],
        },
    }


@pytest.fixture
def response_body():
    return {
        "model": "jev-test-resolved",
        "answers": {
            "department": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.8, "technical": 0.2},
                "confidence": 0.6,
            },
            "urgent": {"type": "noul", "noul": 0.91},
            "severity": {
                "type": "score",
                "score": 0.75,
                "legend": {"0": "minor", "1": {"impact": ["service unavailable"]}},
                "probabilities": {"0": 0.25, "1": 0.75},
                "confidence": 0.5,
            },
        },
        "usage": {"input_tokens": 37, "output_tokens": 11},
    }


class TypeSafeBoundary:
    def __init__(self, response_body):
        self.response_body = response_body
        self.requests = []
        self.transports = []
        self.handler = None

    async def respond(self, request):
        self.requests.append(request)
        if self.handler is not None:
            return await self.handler(request)
        return httpx2.Response(200, json=self.response_body)


@pytest.fixture
def service(monkeypatch, response_body):
    boundary = TypeSafeBoundary(response_body)
    real_client = typesafe_sdk.AsyncTypeSafeClient

    class Transport(httpx2.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True
            await super().aclose()

    def make_client(**kwargs):
        transport = Transport(boundary.respond)
        boundary.transports.append(transport)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-key")
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", make_client)
    return boundary


@pytest.mark.parametrize(
    "declaration",
    [
        {},
        {"q": {"type": "unknown", "instructions": "Decide"}},
        {"q": {"type": "noul", "instructions": 3}},
        {"q": {"type": "noul", "instructions": {"nested": [object()]}}},
        {"q": {"type": "choice", "instructions": "Pick", "criteria": {}}},
        {"q": {"type": "score", "instructions": "Rate", "criteria": ["only"]}},
        {"q": {"type": "noul", "instructions": "Decide", "criteria": {"maybe": "?"}}},
        {"q": {"type": "noul", "instructions": "Decide", "unexpected": True}},
    ],
    ids=["empty", "type", "scalar", "nested-non-json", "options", "levels", "noul", "extra"],
)
def test_invalid_declaration_fails_without_creating_client(monkeypatch, declaration):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    def forbidden_client(**kwargs):
        pytest.fail("declaration validation must precede client creation")

    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", forbidden_client)
    with pytest.raises(ClassifierStepError):

        @ava.classifier_step(questions=declaration)
        async def classify(*, classifier: ava.Classifier):
            return await classifier(state="ticket")


def test_classifier_parameter_must_be_keyword_only(questions):
    with pytest.raises(ClassifierStepError):

        @ava.classifier_step(questions=questions)
        async def classify(classifier: ava.Classifier):
            return await classifier(state="ticket")


@pytest.mark.asyncio
async def test_nested_declaration_is_owned_and_all_answer_types_survive(
    questions, response_body, service
):
    original = copy.deepcopy(questions)

    @ava.classifier_step(questions=questions)
    async def classify(*, classifier: ava.Classifier):
        return await classifier(state={"ticket": ["charged twice"]})

    questions["department"]["instructions"]["context"].append("changed")
    questions["department"]["criteria"]["billing"]["examples"].clear()
    questions["severity"]["criteria"][1]["impact"][0] = "mutated"
    observed = []
    with capture_classifier_evidence(observed.append):
        result = await classify.fn()

    assert result.choices["department"].choice == "billing"
    assert result.nouls["urgent"].noul == 0.91
    assert result.scores["severity"].score == 0.75
    assert result.model_dump(mode="json") == response_body
    assert set(result.choices) == {"department"}
    assert set(result.nouls) == {"urgent"}
    assert set(result.scores) == {"severity"}
    wire_questions = json.loads(service.requests[0].content)["questions"]
    assert wire_questions["department"]["instructions"]["context"] == ["customer support"]
    assert wire_questions["department"]["criteria"]["billing"]["examples"] == [
        "duplicate charge"
    ]
    assert wire_questions["severity"]["criteria"][1]["impact"] == ["service unavailable"]
    assert observed[-1].declaration.model_dump(mode="json")["questions"] == original
    assert [record.status for record in observed] == ["running", "success"]
    assert observed[0].result is None
    assert all(record.input == {"ticket": ["charged twice"]} for record in observed)
    assert observed[0].ended_at is None
    assert observed[-1].ended_at >= observed[0].started_at
    assert all(transport.closed for transport in service.transports)


def test_workflow_defaults_do_not_leak_and_step_overrides_win(questions, service):
    @ava.source
    def load():
        return "charged twice"

    @ava.classifier_step(questions=questions)
    async def inherited(ticket, *, classifier: ava.Classifier):
        return await classifier(state=ticket)

    @ava.classifier_step(questions=questions, model="step-model", timeout=3.0)
    async def overridden(ticket, *, classifier: ava.Classifier):
        return await classifier(state=ticket)

    @ava.workflow(classifier_defaults={"model": "workflow-a", "timeout": 7.0})
    def flow_a():
        return inherited(load())

    @ava.workflow(classifier_defaults={"model": "workflow-b", "timeout": 9.0})
    def flow_b():
        return inherited(load())

    @ava.workflow(classifier_defaults={"model": "workflow-c", "timeout": 11.0})
    def flow_override():
        return overridden(load())

    for flow in (flow_a, flow_b, flow_override, flow_a):
        result = flow().run(executor=ava.LocalExecutor()).result()
        assert result.choices["department"].choice == "billing"
    assert [json.loads(request.content)["model"] for request in service.requests] == [
        "workflow-a",
        "workflow-b",
        "step-model",
        "workflow-a",
    ]
    assert [request.extensions["timeout"]["read"] for request in service.requests] == [
        7.0,
        9.0,
        3.0,
        7.0,
    ]
    assert all(transport.closed for transport in service.transports)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    ["missing", "extra", "type", "choice-label", "choice-options", "score-levels", "legend"],
)
async def test_response_must_match_exact_declared_questions(questions, service, mismatch):
    answers = service.response_body["answers"]
    if mismatch == "missing":
        del answers["urgent"]
    elif mismatch == "extra":
        answers["surprise"] = {"type": "noul", "noul": 0.5}
    elif mismatch == "type":
        answers["urgent"] = copy.deepcopy(answers["department"])
    elif mismatch == "choice-label":
        answers["department"]["choice"] = "sales"
    elif mismatch == "choice-options":
        answers["department"]["probabilities"] = {"billing": 0.8, "sales": 0.2}
    elif mismatch == "score-levels":
        answers["severity"]["probabilities"] = {"0": 0.25, "2": 0.75}
    else:
        answers["severity"]["legend"]["1"] = "different rubric"

    @ava.classifier_step(questions=questions)
    async def classify(*, classifier: ava.Classifier):
        return await classifier(state="ticket")

    observed = []
    with capture_classifier_evidence(observed.append):
        with pytest.raises(ClassifierStepExecutionError):
            await classify.fn()
    assert [record.status for record in observed] == ["running", "failed"]
    assert observed[-1].result is None
    assert observed[-1].error
    assert observed[0].invocation_id == observed[-1].invocation_id
    assert all(record.input == "ticket" for record in observed)
    assert all(transport.closed for transport in service.transports)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_state", [None, {"bad": float("nan")}])
async def test_invalid_state_does_not_leak_into_traceback_or_evidence(
    questions, service, invalid_state
):
    secret = "classifier-private-marker"
    state = (
        {"secret": secret, **invalid_state}
        if isinstance(invalid_state, dict)
        else invalid_state
    )

    @ava.classifier_step(questions=questions)
    async def classify(*, classifier: ava.Classifier):
        return await classifier(state=state)

    observed = []
    with capture_classifier_evidence(observed.append):
        try:
            await classify.fn()
        except ClassifierStepExecutionError:
            rendered = traceback.format_exc()
        else:
            pytest.fail("invalid state must fail classification")

    assert secret not in rendered
    assert "ValidationError" in rendered
    assert [record.status for record in observed] == ["running", "failed"]
    assert all(record.input is None for record in observed)
    assert observed[0].invocation_id == observed[-1].invocation_id
    assert observed[-1].result is None
    assert "ValidationError" in observed[-1].error
    assert secret not in "".join(record.model_dump_json() for record in observed)
    assert service.requests == []


@pytest.mark.asyncio
async def test_http_failure_does_not_leak_response_body_or_credentials(
    questions, service, monkeypatch
):
    secret = "classifier-private-marker"
    credential = "classifier-private-credential"
    monkeypatch.setenv("TYPESAFE_API_KEY", credential)

    async def reject(request):
        payload = {
            "state": json.loads(request.content)["state"],
            "authorization": request.headers["authorization"],
        }
        return httpx2.Response(400, json={"message": json.dumps(payload)})

    service.handler = reject

    @ava.classifier_step(questions=questions)
    async def classify(*, classifier: ava.Classifier):
        return await classifier(state={"secret": secret})

    observed = []
    with capture_classifier_evidence(observed.append):
        try:
            await classify.fn()
        except ClassifierStepExecutionError:
            rendered = traceback.format_exc()
        else:
            pytest.fail("unsuccessful HTTP response must fail classification")

    assert "TypeSafeBadRequestError" in rendered
    assert "HTTP 400" in rendered
    assert [record.status for record in observed] == ["running", "failed"]
    assert observed[-1].result is None
    assert "TypeSafeBadRequestError" in observed[-1].error
    assert "HTTP 400" in observed[-1].error
    assert all(record.input == {"secret": secret} for record in observed)
    errors = "".join(record.model_dump_json(exclude={"input"}) for record in observed)
    for value in (secret, credential):
        assert value not in rendered
        assert value not in errors
    assert credential not in "".join(record.model_dump_json() for record in observed)
    assert all(transport.closed for transport in service.transports)


@pytest.mark.asyncio
async def test_invalid_response_does_not_leak_validation_input_or_locations(questions, service):
    secret = "classifier-private-marker"
    service.response_body["answers"][secret] = {"type": "noul", "noul": 2.0}

    @ava.classifier_step(questions=questions)
    async def classify(*, classifier: ava.Classifier):
        return await classifier(state={"secret": secret})

    observed = []
    with capture_classifier_evidence(observed.append):
        try:
            await classify.fn()
        except ClassifierStepExecutionError:
            rendered = traceback.format_exc()
        else:
            pytest.fail("invalid response must fail classification")

    assert "ValidationError" in rendered
    assert secret not in rendered
    assert [record.status for record in observed] == ["running", "failed"]
    assert observed[-1].result is None
    assert "ValidationError" in observed[-1].error
    assert all(record.input == {"secret": secret} for record in observed)
    assert secret not in "".join(
        record.model_dump_json(exclude={"input"}) for record in observed
    )
    assert all(transport.closed for transport in service.transports)


@pytest.mark.asyncio
@pytest.mark.parametrize("primary", ["none", "postprocessing", "cancellation"])
async def test_cleanup_failure_is_private_and_preserves_primary_failure(
    questions, service, monkeypatch, primary
):
    secret = "classifier-private-marker"
    primary_error = (
        asyncio.CancelledError()
        if primary == "cancellation"
        else ValueError("user postprocessing failed")
    )
    user_cause = RuntimeError("user postprocessing cause")

    @ava.classifier_step(questions=questions)
    async def classify(*, classifier: ava.Classifier):
        result = await classifier(state={"secret": secret})
        close_transport = service.transports[0].aclose

        async def fail_close():
            await close_transport()
            raise RuntimeError(secret)

        monkeypatch.setattr(service.transports[0], "aclose", fail_close)
        if primary != "none":
            raise primary_error from user_cause
        return result

    observed = []
    with capture_classifier_evidence(observed.append):
        try:
            await classify.fn()
        except BaseException as error:
            rendered = traceback.format_exc()
            if primary == "none":
                assert isinstance(error, ClassifierStepExecutionError)
            else:
                assert error is primary_error
                assert error.__cause__ is user_cause
                assert str(user_cause) in rendered
        else:
            pytest.fail("cleanup failure must not be silently ignored")

    assert "RuntimeError" in rendered
    assert secret not in rendered
    assert [record.status for record in observed] == ["running", "success"]
    assert all(record.input == {"secret": secret} for record in observed)
    assert secret not in "".join(
        record.model_dump_json(exclude={"input"}) for record in observed
    )
    assert all(transport.closed for transport in service.transports)


@pytest.mark.parametrize("fail_after_classification", [False, True])
def test_body_transformation_or_failure_keeps_original_captured_answers(
    questions, response_body, service, fail_after_classification
):
    class PostprocessingError(Exception):
        pass

    observed = []

    @ava.source
    def load():
        return "private-ticket-state"

    @ava.classifier_step(questions=questions)
    async def classify(ticket, *, classifier: ava.Classifier):
        result = await classifier(state=ticket)
        assert observed[-1].status == "success"
        result.choices["department"].probabilities["billing"] = 0.0
        if fail_after_classification:
            raise PostprocessingError()
        return {
            "queue": result.choices["department"].choice,
            "page": result.nouls["urgent"].noul > 0.9,
        }

    @ava.workflow
    def flow():
        return classify(load())

    with capture_classifier_evidence(observed.append):
        handle = flow().run(executor=ava.LocalExecutor())
        if fail_after_classification:
            with pytest.raises(PostprocessingError):
                handle.result()
        else:
            assert handle.result() == {"queue": "billing", "page": True}
    assert [record.status for record in observed] == ["running", "success"]
    assert observed[-1].result.model_dump(mode="json") == response_body
    assert all(record.input == "private-ticket-state" for record in observed)
    assert all(transport.closed for transport in service.transports)


@pytest.mark.asyncio
async def test_multiple_concurrent_calls_and_step_executions_isolate_evidence(
    questions, service
):
    arrivals = 0
    ready = asyncio.Event()

    async def respond(request):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 4:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=5)
        payload = copy.deepcopy(service.response_body)
        index = json.loads(request.content)["state"]["index"]
        payload["answers"]["urgent"]["noul"] = index / 10
        return httpx2.Response(200, json=payload)

    service.handler = respond

    @ava.classifier_step(questions=questions)
    async def classify(offset, *, classifier: ava.Classifier):
        return await asyncio.gather(
            classifier(state={"index": offset}), classifier(state={"index": offset + 1})
        )

    async def execute(offset):
        records = []
        with capture_classifier_evidence(records.append):
            results = await classify.fn(offset)
        return results, records

    left, right = await asyncio.gather(execute(1), execute(7))
    seen_ids = set()
    for (results, records), indices in ((left, [1, 2]), (right, [7, 8])):
        expected = [index / 10 for index in indices]
        assert [result.nouls["urgent"].noul for result in results] == expected
        grouped = defaultdict(list)
        for record in records:
            grouped[record.invocation_id].append(record)
        assert len(grouped) == 2
        assert not seen_ids.intersection(grouped)
        seen_ids.update(grouped)
        assert [
            record.invocation_index for record in records if record.status == "running"
        ] == [0, 1]
        for events in grouped.values():
            assert [event.status for event in events] == ["running", "success"]
            assert events[0].result is None
            assert events[0].invocation_index == events[1].invocation_index
            expected_input = {"index": indices[events[0].invocation_index]}
            assert events[0].input == events[1].input == expected_input
            assert events[1].result.nouls["urgent"].noul == expected[events[0].invocation_index]
    assert all(transport.closed for transport in service.transports)


@pytest.mark.asyncio
async def test_cancellation_preserves_terminal_status_and_closes_client(questions, service):
    entered = asyncio.Event()

    async def block(request):
        entered.set()
        await asyncio.Future()

    service.handler = block

    @ava.classifier_step(questions=questions)
    async def classify(*, classifier: ava.Classifier):
        return await classifier(state="cancelled ticket")

    observed = []
    with capture_classifier_evidence(observed.append):
        task = asyncio.create_task(classify.fn())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
        finally:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [record.status for record in observed] == ["running", "cancelled"]
    assert observed[0].invocation_id == observed[-1].invocation_id
    assert all(record.input == "cancelled ticket" for record in observed)
    assert observed[-1].result is None
    assert observed[-1].ended_at >= observed[0].started_at
    assert all(transport.closed for transport in service.transports)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["", [], {}])
async def test_empty_valid_state_is_retained_instead_of_null(questions, service, state):
    @ava.classifier_step(questions=questions)
    async def classify(*, classifier: ava.Classifier):
        return await classifier(state=state)

    observed = []
    with capture_classifier_evidence(observed.append):
        await classify.fn()

    assert [record.status for record in observed] == ["running", "success"]
    assert all(record.input == state for record in observed)
    assert json.loads(service.requests[0].content)["state"] == state


@pytest.mark.asyncio
async def test_input_snapshot_is_detached_before_send_and_after_return(questions, service):
    state = {"ticket": [{"message": "original"}]}
    observed = []

    def capture(record):
        observed.append(record)
        if record.status == "running":
            state["ticket"][0]["message"] = "changed before request"

    async def respond(request):
        state["ticket"].append({"message": "changed while in flight"})
        return httpx2.Response(200, json=service.response_body)

    service.handler = respond

    @ava.classifier_step(questions=questions)
    async def classify(*, classifier: ava.Classifier):
        result = await classifier(state=state)
        state["ticket"].clear()
        return result

    with capture_classifier_evidence(capture):
        await classify.fn()

    expected = {"ticket": [{"message": "original"}]}
    assert json.loads(service.requests[0].content)["state"] == expected
    assert [record.status for record in observed] == ["running", "success"]
    assert all(record.input == expected for record in observed)
    observed[0].input["ticket"].clear()
    assert observed[-1].input == expected


def test_discovery_without_credentials_preserves_structured_author_order(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "classification.py"
    source.write_text("""
import avalanche as ava
import typesafe_sdk

def forbidden_client(**kwargs):
    raise AssertionError("discovery constructed a service client")
typesafe_sdk.AsyncTypeSafeClient = forbidden_client

@ava.source
def load():
    return "ticket"

@ava.classifier_step(questions={
    "z_last_alphabetically": {"type": "noul", "instructions": {"ask": ["Is urgent?"]}},
    "a_first_alphabetically": {
        "type": "score", "instructions": "Rate", "criteria": ["low", "high"],
    },
})
async def classify(ticket, *, classifier: ava.Classifier):
    return await classifier(state=ticket)

@ava.workflow(classifier_defaults={"model": "discovery-model", "timeout": 4.0})
def discovered():
    return classify(load())
""")
    registry = WorkflowRegistry()
    registry.scan([str(source)])
    [workflow] = registry.descriptors()
    [(_, metadata_json)] = workflow.classifier_metadata_json
    metadata = json.loads(metadata_json)
    assert list(metadata["questions"]) == ["z_last_alphabetically", "a_first_alphabetically"]
    assert metadata["questions"]["z_last_alphabetically"]["instructions"] == {
        "ask": ["Is urgent?"]
    }
    assert metadata["runtime"] == {"model": "discovery-model", "timeout": 4.0}


def test_classification_result_is_a_serializable_workflow_final_value(
    questions, response_body, service
):
    @ava.source
    def load():
        return ["ticket", {"account": "example"}]

    @ava.classifier_step(questions=questions)
    async def classify(ticket, *, classifier: ava.Classifier):
        return await classifier(state=ticket)

    @ava.workflow
    def flow():
        return classify(load())

    result = flow().run(executor=ava.LocalExecutor()).result()
    assert isinstance(result, ava.ClassificationResult)
    assert json.loads(result.model_dump_json()) == response_body
    restored = ava.ClassificationResult.model_validate_json(result.model_dump_json())
    assert restored.scores["severity"].score == 0.75
    assert restored.choices["department"].probabilities == {"billing": 0.8, "technical": 0.2}
