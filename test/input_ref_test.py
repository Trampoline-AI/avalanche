import pytest
from pydantic import BaseModel

import avalanche as ava


class NestedInput(BaseModel):
    value: str


class RunInput(ava.BaseInput):
    question: str
    nested: NestedInput


def test_input_selectors_resolve_validated_models_in_positional_and_keyword_slots():
    @ava.step
    def collect(question, *, deep, payload):
        return question + ":" + deep, payload

    @ava.workflow(input=RunInput)
    def flow():
        return collect(ava.input.question, deep=ava.input.nested.value, payload=ava.input)

    result, payload = (
        flow()
        .run(
            executor=ava.LocalExecutor(),
            input={"question": "hi", "nested": {"value": "deep"}},
        )
        .result(timeout=5)
    )
    assert result == "hi:deep"
    assert payload == RunInput(question="hi", nested=NestedInput(value="deep"))


def test_missing_input_and_missing_attribute_fail_before_invoking_the_node():
    @ava.step
    def collect(value):
        raise AssertionError("unresolved input must not reach user code")

    @ava.workflow
    def no_input():
        return collect(ava.input.question)

    with pytest.raises(ValueError, match="input"):
        no_input().run(executor=ava.LocalExecutor()).result(timeout=5)

    @ava.workflow(input=RunInput)
    def missing_attribute():
        return collect(ava.input.nope)

    with pytest.raises(AttributeError, match=r"collect.*nope"):
        missing_attribute().run(
            executor=ava.LocalExecutor(),
            input={"question": "hi", "nested": {"value": "deep"}},
        ).result(timeout=5)
