"""SignatureSpec: the runtime-neutral view of a DSPy agent signature."""

from __future__ import annotations

import datetime
import enum

import dspy
import pytest
from pydantic import BaseModel, ValidationError

import avalanche as ava
from avalanche.agent.contract import (
    AgentRequest,
    JsonOutputs,
    PythonOutputs,
    SignatureSpec,
    ToolSpec,
)


class Priority(enum.Enum):
    LOW = "low"
    HIGH = "high"


class Ticket(BaseModel):
    title: str
    due: datetime.date
    priority: Priority


class Triage(ava.Signature):
    """Triage the supplied document."""

    document: ava.File = ava.InputField(desc="the document to triage")
    workspace: ava.Workspace = ava.InputField()
    ticket: Ticket = ava.OutputField(desc="the ticket to file")
    estimate_hours: int = ava.OutputField(ge=0)


def test_from_dspy_keeps_name_instructions_and_field_contracts():
    spec = SignatureSpec.from_dspy(Triage)

    assert (spec.name, spec.instructions) == ("Triage", "Triage the supplied document.")
    assert [(field.name, field.description) for field in spec.inputs] == [
        ("document", "the document to triage"),
        ("workspace", None),
    ]
    assert [(field.name, field.description) for field in spec.outputs] == [
        ("ticket", "the ticket to file"),
        ("estimate_hours", None),
    ]
    estimate = spec.outputs[1].json_schema
    assert (estimate["type"], estimate["minimum"]) == ("integer", 0)


def test_from_dspy_accepts_inline_string_signatures():
    spec = SignatureSpec.from_dspy(ava.Signature("question: str -> answer: int"))

    assert [field.name for field in spec.inputs] == ["question"]
    assert spec.validate_outputs(JsonOutputs(text='{"answer": 3}')).model_dump() == {
        "answer": 3
    }


def test_from_dspy_rejects_signatures_without_outputs():
    with pytest.raises(TypeError, match="no output fields"):
        SignatureSpec.from_dspy(dspy.Signature({"question": (str, ava.InputField())}))


@pytest.mark.parametrize(
    "field",
    [
        # DSPy still records its deprecated, no-op ``format`` option.
        pytest.param(
            lambda: ava.OutputField(format=str),
            id="unsupported-option",
            marks=pytest.mark.filterwarnings("ignore::DeprecationWarning"),
        ),
        pytest.param(lambda: ava.OutputField(default=1), id="default"),
    ],
)
def test_from_dspy_rejects_field_options_it_cannot_carry(field):
    signature = dspy.Signature({"question": (str, ava.InputField()), "answer": (int, field())})

    with pytest.raises(TypeError, match="answer"):
        SignatureSpec.from_dspy(signature)


def test_python_outputs_validate_strictly():
    spec = SignatureSpec.from_dspy(Triage)
    ticket = Ticket(title="t", due=datetime.date(2026, 1, 2), priority=Priority.HIGH)

    outputs = spec.validate_outputs(
        PythonOutputs(values={"ticket": ticket, "estimate_hours": 2})
    )

    assert isinstance(outputs, spec.output_model)
    assert outputs.model_dump() == {"ticket": ticket.model_dump(), "estimate_hours": 2}
    with pytest.raises(ValidationError):
        spec.validate_outputs(PythonOutputs(values={"ticket": ticket, "estimate_hours": "2"}))


def test_json_outputs_accept_json_encodings_of_dates_and_enums():
    spec = SignatureSpec.from_dspy(Triage)

    outputs = spec.validate_outputs(
        JsonOutputs(
            text='{"ticket": {"title": "t", "due": "2026-01-02", "priority": "high"},'
            ' "estimate_hours": 2}'
        )
    )

    assert outputs.model_dump()["ticket"] == {
        "title": "t",
        "due": datetime.date(2026, 1, 2),
        "priority": Priority.HIGH,
    }


@pytest.mark.parametrize(
    "text",
    [
        '{"ticket": {"title": "t", "due": "2026-01-02", "priority": "high"}}',
        '{"ticket": {"title": "t", "due": "2026-01-02", "priority": "high"},'
        ' "estimate_hours": -1}',
        '{"ticket": {"title": "t", "due": "2026-01-02", "priority": "high"},'
        ' "estimate_hours": 2, "extra": 1}',
    ],
    ids=["missing-field", "constraint", "extra-field"],
)
def test_json_outputs_reject_contract_violations(text):
    with pytest.raises(ValidationError):
        SignatureSpec.from_dspy(Triage).validate_outputs(JsonOutputs(text=text))


def test_request_passes_file_and_workspace_inputs_unchanged(tmp_path):
    spec = SignatureSpec.from_dspy(Triage)
    source = tmp_path / "a.txt"
    source.write_bytes(b"hello")
    document = ava.File.from_path(source)
    workspace = ava.Workspace()

    def lookup(term: str) -> str:
        """Look a term up."""
        return term

    request = AgentRequest(
        invocation_id="i-1",
        step_name="triage",
        signature=spec,
        inputs=spec.input_model(document=document, workspace=workspace),
        tools=(ToolSpec.from_callable(lookup),),
    )

    assert request.inputs.document is document
    assert request.inputs.workspace is workspace
    assert (request.tools[0].name, request.tools[0].description) == (
        "lookup",
        "Look a term up.",
    )


def test_request_rejects_inputs_of_another_model():
    spec = SignatureSpec.from_dspy(Triage)
    other = SignatureSpec.from_dspy(ava.Signature("question: str -> answer: int"))

    with pytest.raises(ValidationError, match="TriageInputs"):
        AgentRequest(
            invocation_id="i-1",
            step_name="triage",
            signature=spec,
            inputs=other.input_model(question="q"),
            tools=(),
        )
