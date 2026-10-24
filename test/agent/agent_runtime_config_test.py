"""Choosing an agent step's runtime: step runtime, workflow default, or PredictRLM."""

from __future__ import annotations

import pytest

import avalanche as ava
from avalanche.agent.declaration import PredictRLMConfiguredModel, PredictRLMDefaultModel
from avalanche.agent.defaults import AgentDefaults, effective_runtime
from avalanche.runtimes import PredictRLM


class Echo(ava.Signature):
    """Echo the text."""

    text: str = ava.InputField()
    answer: str = ava.OutputField()


class OtherRuntime:
    """A second runtime kind, as far as runtime selection can tell."""

    kind = "other"

    def merged_over(self, base):
        raise AssertionError("runtimes of different kinds never merge")

    def declaration(self, *, workflow_default):
        raise NotImplementedError

    async def invoke(self, request, events):
        raise NotImplementedError


@pytest.mark.parametrize(
    ("step", "default", "expected"),
    [
        (None, None, PredictRLM()),
        (None, PredictRLM(lm="workflow"), PredictRLM(lm="workflow")),
        (PredictRLM(max_iterations=3), None, PredictRLM(max_iterations=3)),
        (
            PredictRLM(lm="step", max_iterations=3),
            PredictRLM(lm="workflow", sub_lm="workflow-sub"),
            PredictRLM(lm="step", sub_lm="workflow-sub", max_iterations=3),
        ),
    ],
    ids=["neither", "workflow-only", "step-only", "merged-field-by-field"],
)
def test_effective_runtime(step, default, expected):
    assert effective_runtime(step, AgentDefaults(runtime=default)) == expected


def test_a_step_runtime_of_another_kind_replaces_the_workflow_default():
    class Other(OtherRuntime):
        def merged_over(self, base):
            return self

    other = Other()

    assert effective_runtime(other, AgentDefaults(runtime=PredictRLM(lm="x"))) is other


@pytest.mark.parametrize("where", ["step", "workflow"])
def test_each_agent_gets_its_own_runtime_instance(where):
    declared = PredictRLM(lm="x")
    step = declared if where == "step" else None
    defaults = AgentDefaults(runtime=declared if where == "workflow" else None)

    first, second = effective_runtime(step, defaults), effective_runtime(step, defaults)

    assert first == declared and second == declared
    assert first is not declared and second is not first


def test_merged_runtime_remembers_only_fields_someone_set():
    merged = PredictRLM(max_iterations=3).merged_over(PredictRLM(lm="workflow"))

    assert merged.model_fields_set == {"lm", "max_iterations"}
    assert merged.verbose is False


def test_declaration_names_where_each_model_came_from():
    @ava.agent_step(Echo, runtime=PredictRLM(sub_lm="step-sub"))
    async def echo(text: str, *, agent: ava.Agent) -> str:
        return (await agent(text=text)).outputs.answer

    declaration = echo.fn.__agent_step__.declaration(
        AgentDefaults(runtime=PredictRLM(lm="workflow-main", max_iterations=4))
    )

    assert declaration.runtime.models.main == PredictRLMConfiguredModel(
        source="workflow default", identity="workflow-main"
    )
    assert declaration.runtime.models.sub == PredictRLMConfiguredModel(
        source="step override", identity="step-sub"
    )
    assert declaration.runtime.settings["max_iterations"] == 4


def test_declaration_attributes_inherited_models_to_the_workflow():
    @ava.agent_step(Echo)
    async def echo(text: str, *, agent: ava.Agent) -> str:
        return (await agent(text=text)).outputs.answer

    runtime = echo.fn.__agent_step__.declaration(
        AgentDefaults(runtime=PredictRLM(lm="workflow-main"))
    ).runtime

    assert runtime.models.main == PredictRLMConfiguredModel(
        source="workflow default", identity="workflow-main"
    )


def test_declaration_without_runtime_or_defaults_uses_predict_rlm_defaults():
    @ava.agent_step(Echo)
    async def echo(text: str, *, agent: ava.Agent) -> str:
        return (await agent(text=text)).outputs.answer

    runtime = echo.fn.__agent_step__.declaration().runtime

    assert runtime.models.main == PredictRLMDefaultModel()
    assert runtime.settings["max_iterations"] == PredictRLM().max_iterations



def test_extra_options_merge_like_fields_with_the_step_winning():
    merged = PredictRLM(future_limit=3).merged_over(
        PredictRLM(future_limit=1, future_mode="fast")
    )

    assert merged.model_extra == {"future_limit": 3, "future_mode": "fast"}


def test_declaration_names_extra_options_and_keeps_only_json_values():
    @ava.agent_step(Echo, runtime=PredictRLM(future_limit=3, future_hook=print))
    async def echo(text: str, *, agent: ava.Agent) -> str:
        return (await agent(text=text)).answer

    runtime = echo.fn.__agent_step__.declaration(
        AgentDefaults(runtime=PredictRLM(future_mode="fast"))
    ).runtime

    assert runtime.extra_options == ("future_hook", "future_limit", "future_mode")
    assert runtime.settings["future_limit"] == 3
    assert runtime.settings["future_mode"] == "fast"
    assert "future_hook" not in runtime.settings

@pytest.mark.parametrize(
    "removed",
    [{"lm": "x"}, {"sub_lm": "x", "max_iterations": 2}, {"skills": []}, {"verbose": True}],
)
def test_removed_step_arguments_name_their_replacement(removed):
    with pytest.raises(TypeError, match=r"runtime=ava\.runtimes\.PredictRLM\(") as raised:
        ava.agent_step(Echo, **removed)
    for name in removed:
        assert f"{name}=" in str(raised.value)

    @ava.workflow
    def flow():
        return ava.agent.step(Echo, inputs={"text": "hi"}, **removed)

    with pytest.raises(TypeError, match=r"runtime=ava\.runtimes\.PredictRLM\("):
        flow()


@pytest.mark.parametrize("name", ["tools", "lm_model"])
def test_workflow_default_keys_that_are_not_runtime_settings_get_no_runtime_hint(name):
    with pytest.raises(TypeError, match="unexpected") as raised:
        ava.workflow(agent_defaults={name: "x"})
    assert "PredictRLM(" not in str(raised.value)


def test_unknown_step_keywords_get_no_runtime_hint():
    with pytest.raises(TypeError, match="unexpected keyword arguments lm_model=") as raised:
        ava.agent_step(Echo, lm_model="x")
    assert "PredictRLM(" not in str(raised.value)


def test_removed_workflow_default_keys_name_their_replacement():
    with pytest.raises(TypeError, match=r"\{'runtime': ava\.runtimes\.PredictRLM\(lm=\.\.\."):
        ava.workflow(agent_defaults={"lm": "x"})


@pytest.mark.parametrize("runtime", ["openai/gpt-5.5", {"lm": "x"}])
def test_runtime_must_be_an_agent_runtime(runtime):
    with pytest.raises(TypeError, match="agent runtime"):
        ava.agent_step(Echo, runtime=runtime)
    with pytest.raises(TypeError, match="agent runtime"):
        ava.workflow(agent_defaults={"runtime": runtime})
