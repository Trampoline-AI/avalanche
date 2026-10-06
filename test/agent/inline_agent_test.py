"""Inline agents execute named, validated dataflow without model/network calls."""

from __future__ import annotations

import importlib

import dspy
import pytest
from predict_rlm import RunEvidence, RunTrace
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

import avalanche as ava
from avalanche.agent import AgentStepError

agent_module = importlib.import_module("avalanche.agent.agent_step")


class Item(BaseModel):
    name: str
    quantity: int = Field(gt=0)


class Items(BaseModel):
    customer: str
    items: list[Item]


class ExtractItems(ava.Signature):
    transcript: str = ava.InputField()
    customer: str = ava.InputField()
    extracted: Items = ava.OutputField()


class SplitText(ava.Signature):
    text: str = ava.InputField()
    first: str = ava.OutputField()
    count: int = ava.OutputField()


class PredictionMethodOutputs(ava.Signature):
    text: str = ava.InputField()
    items: list[Item] = ava.OutputField()
    keys: list[str] = ava.OutputField()
    values: tuple[int, ...] = ava.OutputField()
    get: dict[str, int] = ava.OutputField()


class Echo(ava.Signature):
    text: str = ava.InputField()
    answer: str = ava.OutputField()


class Ready(ava.Signature):
    answer: str = ava.OutputField()


class ListTags(ava.Signature):
    tags: list[str] = ava.OutputField()


class TupleTags(ava.Signature):
    tags: tuple[str, ...] = ava.OutputField()


class PositiveCount(ava.Signature):
    text: str = ava.InputField()
    count: int = ava.OutputField(gt=0)


class DefaultCount(ava.Signature):
    text: str = ava.InputField()
    count: int = ava.OutputField(default=2)


class CheckedText(BaseModel):
    model_config = ConfigDict(revalidate_instances="always")
    text: str

    @model_validator(mode="after")
    def mark_validated(self):
        self.text += "!"
        return self


class CheckedOutput(ava.Signature):
    answer: CheckedText = ava.OutputField()


class ConfigInputs(ava.Signature):
    lm: str = ava.InputField()
    tools: str = ava.InputField()
    max_iterations: int = ava.InputField()
    answer: str = ava.OutputField()


@pytest.fixture
def predictor_factory(monkeypatch):
    def install(evaluate):
        class Predictor:
            async def acall(self, **inputs):
                prediction = evaluate(self.signature, self.config, inputs)
                prediction.trace = RunTrace(
                    status="completed",
                    model="test-model",
                    iterations=0,
                    max_iterations=1,
                    duration_ms=1,
                )
                prediction.evidence = RunEvidence(
                    run_id="inline-run", complete=True, terminal_outcome="completed"
                )
                return prediction

        def build(signature, **config):
            predictor = Predictor()
            predictor.signature = signature
            predictor.config = config
            return predictor

        monkeypatch.setattr(agent_module, "_build_predictor", build)

    return install


@pytest.fixture
def extract_predictor(predictor_factory):
    def extract(signature, config, inputs):
        quantity, name = inputs["transcript"].split()
        return dspy.Prediction(
            extracted=Items(
                customer=inputs["customer"],
                items=[Item(name=name, quantity=int(quantity))],
            )
        )

    predictor_factory(extract)


@pytest.mark.parametrize("binding", ["explicit", "ordered", "fan_in"])
def test_pydantic_single_output_preserves_named_and_ordered_inputs(extract_predictor, binding):
    @ava.source(num_returns=2)
    def pair():
        return "3 apples", "Ada"

    @ava.source
    def recording():
        return "3 apples"

    @ava.source
    def account():
        return "Ada"

    @ava.workflow
    def flow():
        if binding == "explicit":
            recorded_words = recording()
            account_name = account()
            return ava.agent.step(
                ExtractItems,
                inputs={"customer": account_name, "transcript": recorded_words},
            )
        if binding == "ordered":
            return pair() >> ava.agent.step(ExtractItems)
        return (recording() & account()) >> ava.agent.step(ExtractItems)

    assert flow().run(executor=ava.LocalExecutor()).result() == Items(
        customer="Ada", items=[Item(name="apples", quantity=3)]
    )


@pytest.mark.parametrize(
    ("signature", "tags"),
    [
        (ListTags, []),
        (ListTags, ["a"]),
        (ListTags, ["a", "b"]),
        (TupleTags, ()),
        (TupleTags, ("a",)),
        (TupleTags, ("a", "b")),
    ],
)
@pytest.mark.parametrize("binding", ["chain", "fan_in", "dependency_ids", "explicit"])
def test_single_collection_output_preserves_its_field_boundary(
    predictor_factory, signature, tags, binding
):
    predictor_factory(lambda signature, config, inputs: dspy.Prediction(tags=tags))

    @ava.source
    def prefix() -> str:
        return "tags"

    @ava.dest
    def consume(tags):
        return type(tags).__name__, tags

    @ava.dest
    def combine(prefix: str, tags):
        return prefix, type(tags).__name__, tags

    @ava.workflow
    def flow():
        produced = ava.agent.step(signature)
        if binding == "explicit":
            return consume(tags=produced)
        if binding == "fan_in":
            return (prefix() & produced) >> combine()
        consumer = consume()
        result = produced >> consumer
        if binding == "dependency_ids":
            consumer._incoming_refs.clear()
        return result

    expected = (type(tags).__name__, tags)
    if binding == "fan_in":
        expected = ("tags", *expected)
    assert flow().run(executor=ava.LocalExecutor()).result() == expected


@pytest.mark.parametrize("signature", [ListTags, TupleTags])
def test_indexed_single_collection_output_still_selects_an_element(
    predictor_factory, signature
):
    tags = ["first", "second"] if signature is ListTags else ("first", "second")
    predictor_factory(lambda signature, config, inputs: dspy.Prediction(tags=tags))

    @ava.dest
    def consume(tag: str) -> str:
        return tag.upper()

    @ava.workflow
    def flow():
        return ava.agent.step(signature)[1] >> consume()

    assert flow().run(executor=ava.LocalExecutor()).result() == "SECOND"


@pytest.mark.parametrize("binding", ["indexed", "automatic"])
def test_multiple_outputs_keep_signature_order_when_consumed(predictor_factory, binding):
    predictor_factory(
        lambda signature, config, inputs: dspy.Prediction(
            first=inputs["text"].split()[0].upper(), count=len(inputs["text"].split())
        )
    )

    @ava.dest
    def render(first: str, count: int):
        return f"{first}:{count * 10}"

    @ava.workflow
    def flow():
        split = ava.agent.step(SplitText, inputs={"text": "red green blue"})
        if binding == "indexed":
            return render(count=split[1], first=split[0])
        return split >> render()

    assert flow().run(executor=ava.LocalExecutor()).result() == "RED:30"


@pytest.mark.parametrize("binding", ["indexed", "automatic"])
def test_prediction_method_outputs_reach_typed_consumers_in_order(predictor_factory, binding):
    predictor_factory(
        lambda signature, config, inputs: dspy.Prediction(
            get={"apple": 5},
            values=(3, 4),
            keys=["apple"],
            items=[Item(name=inputs["text"], quantity=2)],
        )
    )

    @ava.dest
    def receipt(
        items: list[Item], keys: list[str], values: tuple[int, ...], get: dict[str, int]
    ):
        return items[0].name, items[0].quantity * get[keys[0]] + sum(values)

    @ava.workflow
    def flow():
        outputs = ava.agent.step(PredictionMethodOutputs, inputs={"text": "apple"})
        if binding == "indexed":
            return receipt(get=outputs[3], values=outputs[2], keys=outputs[1], items=outputs[0])
        return outputs >> receipt()

    assert flow().run(executor=ava.LocalExecutor()).result() == ("apple", 17)


@pytest.mark.parametrize("missing", ["items", "keys", "values", "get"])
def test_missing_prediction_method_output_fails_as_missing_field(predictor_factory, missing):
    def predict(signature, config, inputs):
        outputs = {
            "items": [Item(name=inputs["text"], quantity=2)],
            "keys": ["apple"],
            "values": (3, 4),
            "get": {"apple": 5},
        }
        del outputs[missing]
        return dspy.Prediction(**outputs)

    predictor_factory(predict)

    @ava.workflow
    def flow():
        return ava.agent.step(PredictionMethodOutputs, inputs={"text": "apple"})

    with pytest.raises(AgentStepError, match=f"missing output field '{missing}'"):
        flow().run(executor=ava.LocalExecutor()).result()


@pytest.mark.parametrize("value", [0, -1, "4", None])
def test_invalid_output_is_not_coerced_or_replaced(predictor_factory, value):
    predictor_factory(lambda signature, config, inputs: dspy.Prediction(count=value))

    @ava.workflow
    def flow():
        return ava.agent.step(PositiveCount, inputs={"text": "four"})

    with pytest.raises(ValidationError):
        flow().run(executor=ava.LocalExecutor()).result()


def test_valid_constrained_output_is_returned(predictor_factory):
    predictor_factory(lambda signature, config, inputs: dspy.Prediction(count=4))

    @ava.workflow
    def flow():
        return ava.agent.step(PositiveCount, inputs={"text": "four"})

    assert flow().run(executor=ava.LocalExecutor()).result() == 4


@pytest.mark.parametrize("quantity", [0, "3"])
def test_invalid_structured_output_is_rejected(predictor_factory, quantity):
    predictor_factory(
        lambda signature, config, inputs: dspy.Prediction(
            extracted={"customer": "Ada", "items": [{"name": "apples", "quantity": quantity}]}
        )
    )

    @ava.workflow
    def flow():
        return ava.agent.step(
            ExtractItems, inputs={"transcript": "0 apples", "customer": "Ada"}
        )

    with pytest.raises(ValidationError):
        flow().run(executor=ava.LocalExecutor()).result()


def test_output_model_validation_runs_once_at_prediction_boundary(predictor_factory):
    predictor_factory(
        lambda signature, config, inputs: dspy.Prediction(answer={"text": "ready"})
    )

    @ava.workflow
    def flow():
        return ava.agent.step(CheckedOutput)

    assert flow().run(executor=ava.LocalExecutor()).result().text == "ready!"


@pytest.mark.parametrize("signature", [Echo, DefaultCount])
def test_missing_output_fails_without_retry(predictor_factory, signature):
    invocations = []

    def predict(signature, config, inputs):
        invocations.append(inputs["text"])
        return dspy.Prediction()

    predictor_factory(predict)

    @ava.workflow
    def flow():
        return ava.agent.step(signature, inputs={"text": "missing"})

    with pytest.raises(AgentStepError):
        flow().run(executor=ava.LocalExecutor()).result()
    assert invocations == ["missing"]


@pytest.mark.parametrize(
    ("inputs", "error"),
    [
        ({}, (AgentStepError, TypeError)),
        ({"unknown": "text"}, AgentStepError),
        ({"text": "text", "extra": 1}, AgentStepError),
    ],
)
def test_missing_and_unknown_explicit_inputs_fail_before_prediction(
    predictor_factory, inputs, error
):
    def unexpected_prediction(signature, config, received):
        raise AssertionError("invalid inputs reached predictor")

    predictor_factory(unexpected_prediction)

    @ava.workflow
    def flow():
        return ava.agent.step(Echo, inputs=inputs)

    with pytest.raises(error):
        flow().run(executor=ava.LocalExecutor()).result()


def test_extra_upstream_inputs_are_rejected(predictor_factory):
    def unexpected_prediction(signature, config, inputs):
        raise AssertionError("extra inputs reached predictor")

    predictor_factory(unexpected_prediction)

    @ava.source(num_returns=2)
    def pair():
        return "hello", "unexpected"

    @ava.workflow
    def flow():
        return pair() >> ava.agent.step(Echo)

    with pytest.raises((AgentStepError, TypeError)):
        flow().run(executor=ava.LocalExecutor()).result()


def test_zero_input_signature_and_lazy_predictor_creation(predictor_factory):
    predictor_factory(lambda signature, config, inputs: dspy.Prediction(answer="ready"))

    @ava.workflow
    def flow():
        return ava.agent.step(Ready)

    # A declaration may be inspected without having a usable model/predictor.
    with pytest.MonkeyPatch.context() as patch:

        def forbidden_build(*args, **kwargs):
            raise AssertionError("predictor built while declaring workflow")

        patch.setattr(agent_module, "_build_predictor", forbidden_build)
        declared = flow()
    assert declared.run(executor=ava.LocalExecutor()).result() == "ready"


def test_signature_inputs_can_share_configuration_parameter_names(predictor_factory):
    def predict(signature, config, inputs):
        return dspy.Prediction(
            answer=(inputs["lm"] + "/" + inputs["tools"]) * inputs["max_iterations"]
        )

    predictor_factory(predict)

    @ava.workflow
    def flow():
        return ava.agent.step(
            ConfigInputs,
            inputs={"lm": "x", "tools": "y", "max_iterations": 2},
            max_iterations=7,
        )

    assert flow().run(executor=ava.LocalExecutor()).result() == "x/yx/y"


def test_workflow_defaults_apply_and_inline_overrides_win(predictor_factory):
    def predict(signature, config, inputs):
        words = inputs["text"].split()[: config["max_iterations"]]
        text = " ".join(words)
        if config["sub_lm"] == "upper":
            text = text.upper()
        return dspy.Prediction(answer=config["lm"] + ":" + text)

    predictor_factory(predict)

    @ava.workflow(agent_defaults={"lm": "default", "sub_lm": "upper", "max_iterations": 2})
    def flow():
        return (
            ava.agent.step(Echo, inputs={"text": "one two three"}),
            ava.agent.step(
                Echo,
                inputs={"text": "one two three"},
                lm="override",
                sub_lm="lower",
                max_iterations=1,
            ),
        )

    assert flow().run(executor=ava.LocalExecutor()).result() == (
        "default:ONE TWO",
        "override:one",
    )


def test_repeated_signatures_are_independent_nodes(predictor_factory):
    predictor_factory(
        lambda signature, config, inputs: dspy.Prediction(answer=inputs["text"].upper())
    )

    @ava.workflow
    def flow():
        first = ava.agent.step(Echo, inputs={"text": "left"})
        second = ava.agent.step(Echo, inputs={"text": "right"})
        return first, second

    assert flow().run(executor=ava.LocalExecutor()).result() == ("LEFT", "RIGHT")


def test_explicit_inline_slug_collisions_are_rejected():
    @ava.workflow
    def flow():
        first = ava.agent.step(Echo, inputs={"text": "left"}, slug="same")
        second = ava.agent.step(Echo, inputs={"text": "right"}, slug="same")
        return first, second

    with pytest.raises(ValueError):
        flow()


def test_explicit_inline_slug_selects_lazy_and_cascading_reruns(predictor_factory):
    predictor_factory(lambda signature, config, inputs: dspy.Prediction(answer="ready"))
    executed = []

    @ava.dest
    def save(value):
        executed.append("save")
        return value + "-saved"

    @ava.source
    def unrelated():
        executed.append("unrelated")
        return "other"

    @ava.workflow
    def flow():
        selected = ava.agent.step(Ready, slug="selected-agent")
        final = selected >> save()
        return selected, final, unrelated()

    workflow = flow()
    assert workflow.run(executor=ava.LocalExecutor(), run_id="original").result() == (
        "ready",
        "ready-saved",
        "other",
    )
    executed.clear()
    assert workflow.run(
        executor=ava.LocalExecutor(),
        rerun=ava.Rerun(run_id="original", start=["selected-agent"], mode="lazy"),
    ).result() == ("ready", None, None)
    assert executed == []
    assert workflow.run(
        executor=ava.LocalExecutor(),
        rerun=ava.Rerun(run_id="original", start=["selected-agent"], mode="autorun"),
    ).result() == ("ready", "ready-saved", None)
    assert executed == ["save"]


def test_bodyful_agent_spellings_and_inline_agents_mix(predictor_factory):
    predictor_factory(
        lambda signature, config, inputs: dspy.Prediction(answer=inputs["text"].upper())
    )

    @ava.agent.step(Echo)
    async def first(text: str, *, agent: ava.Agent):
        return (await agent(text=text)).answer + "!"

    @ava.agent_step(Echo)
    async def second(text: str, *, agent: ava.Agent):
        return (await agent(text=text)).answer + "?"

    @ava.workflow
    def flow():
        return first("hello") >> second() >> ava.agent.step(Echo)

    assert flow().run(executor=ava.LocalExecutor()).result() == "HELLO!?"


@pytest.mark.parametrize("decorator", [ava.agent_step, ava.agent.step])
def test_nested_agent_decorators_fail_with_agent_error(decorator):
    @ava.workflow
    def flow():
        @decorator(Echo)
        async def nested(text: str, *, agent: ava.Agent):
            return (await agent(text=text)).answer

        return nested("hello")

    with pytest.raises(AgentStepError):
        flow()


@pytest.mark.parametrize("decorator", [ava.agent_step, ava.agent.step])
def test_applying_precreated_agent_decorator_inside_workflow_is_rejected(decorator):
    decorate = decorator(Echo)

    @ava.workflow
    def flow():
        @decorate
        async def nested(text: str, *, agent: ava.Agent):
            return (await agent(text=text)).answer

        return nested("hello")

    with pytest.raises(AgentStepError):
        flow()
