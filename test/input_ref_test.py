import pytest
from pydantic import BaseModel

import avalanche as ava


class NestedInput(BaseModel):
    value: str


class RunInput(ava.BaseInput):
    question: str
    nested: NestedInput | None = None


def test_input_ref_resolves_kwarg():
    @ava.step
    def collect(q: str):
        return q

    @ava.workflow(input=RunInput)
    def input_workflow():
        return collect(q=ava.input.question)

    result = input_workflow().run(
        executor=ava.LocalExecutor(),
        input=RunInput(question="hi"),
    )

    assert result == "hi"


def test_input_ref_resolves_positional_arg():
    @ava.step
    def collect(q: str):
        return q

    @ava.workflow(input=RunInput)
    def input_workflow():
        return collect(ava.input.question)

    result = input_workflow().run(
        executor=ava.LocalExecutor(),
        input=RunInput(question="hi"),
    )

    assert result == "hi"


def test_input_ref_resolves_chained_access():
    @ava.step
    def collect(value: str):
        return value

    @ava.workflow(input=RunInput)
    def input_workflow():
        return collect(ava.input.nested.value)

    result = input_workflow().run(
        executor=ava.LocalExecutor(),
        input=RunInput(question="ignored", nested=NestedInput(value="deep")),
    )

    assert result == "deep"


def test_bare_input_ref_resolves_to_run_input_instance():
    @ava.step
    def collect(payload: RunInput):
        return payload

    @ava.workflow(input=RunInput)
    def input_workflow():
        return collect(ava.input)

    result = input_workflow().run(
        executor=ava.LocalExecutor(),
        input=RunInput(question="hi", nested=NestedInput(value="deep")),
    )

    assert isinstance(result, RunInput)
    assert result.question == "hi"
    assert result.nested == NestedInput(value="deep")

def test_bare_input_ref_resolves_keyword_to_run_input_instance():
    @ava.step
    def collect(payload: RunInput):
        return payload.question

    @ava.workflow(input=RunInput)
    def input_workflow():
        return collect(payload=ava.input)

    result = input_workflow().run(
        executor=ava.LocalExecutor(),
        input=RunInput(question="hi"),
    )

    assert result == "hi"


def test_input_ref_missing_attribute_names_node_and_attribute():
    @ava.step
    def collect(q: str):
        return q

    @ava.workflow(input=RunInput)
    def missing_input_workflow():
        return collect(q=ava.input.nope)

    with pytest.raises(AttributeError, match=r"collect.*nope"):
        missing_input_workflow().run(
            executor=ava.LocalExecutor(),
            input=RunInput(question="hi"),
        )


def test_input_ref_without_run_input_raises_value_error():
    @ava.step
    def collect(q: str):
        return q

    @ava.workflow
    def no_input_workflow():
        return collect(q=ava.input.question)

    with pytest.raises(ValueError, match="input"):
        no_input_workflow().run(executor=ava.LocalExecutor())


def test_input_ref_resolves_validated_dict_input():
    @ava.step
    def collect(q: str):
        return q

    @ava.workflow(input=RunInput)
    def input_workflow():
        return collect(q=ava.input.question)

    result = input_workflow().run(
        executor=ava.LocalExecutor(),
        input={"question": "hi", "nested": {"value": "deep"}},
    )

    assert result == "hi"
