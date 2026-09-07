import pytest
from pydantic import BaseModel

import avalanche as ava


class NestedInput(BaseModel):
    value: str


class RunInput(ava.BaseInput):
    question: str
    nested: NestedInput | None = None


def test_input_references_materialize_validated_nested_and_whole_input():
    @ava.step
    def collect(question, *, nested, payload):
        assert isinstance(payload, RunInput)
        return question, nested, payload.nested.value

    @ava.workflow(input=RunInput)
    def flow():
        return collect(ava.input.question, nested=ava.input.nested.value, payload=ava.input)

    assert flow().run(
        executor=ava.LocalExecutor(), input={"question": "hello", "nested": {"value": "deep"}}
    ).result() == ("hello", "deep", "deep")


def test_unresolved_input_reference_fails_without_running_consumer():
    calls = []

    @ava.step
    def collect(value):
        calls.append(value)

    @ava.workflow(input=RunInput)
    def missing_attribute():
        return collect(ava.input.nope)

    @ava.workflow
    def missing_input():
        return collect(ava.input.question)

    with pytest.raises(AttributeError):
        missing_attribute().run(
            executor=ava.LocalExecutor(), input={"question": "hello"}
        ).result()
    with pytest.raises(ValueError):
        missing_input().run(executor=ava.LocalExecutor()).result()
    assert calls == []
