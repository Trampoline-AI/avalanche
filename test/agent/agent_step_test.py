"""Tests for @ava.agent_step wiring (predictor faked; no LM calls)."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import avalanche as ava
from avalanche.agent import (
    AgentStepError,
    AgentStepExecutionError,
    agent_step,
    configure_agent,
    reset_agent_config,
)
from avalanche.dag import Node, NodeType
from avalanche.executor import LocalExecutor
from avalanche.lance import LanceNamespace, LanceNamespaceConfig, LanceTable
from avalanche.lineage import ROW_LINEAGE_COLUMNS
from avalanche.types import AppendResult

# The package attribute `agent_step` (the function) shadows the submodule name,
# so resolve the module itself for monkeypatching the predictor seam.
agent_step_module = importlib.import_module("avalanche.agent.agent_step")


class Person(BaseModel):
    id: int
    name: str


class Summary(BaseModel):
    headline: str
    person_count: int


@pytest.fixture(autouse=True)
def clean_agent_config():
    reset_agent_config()
    yield
    reset_agent_config()


@pytest.fixture
def lance_namespace(tmp_path):
    pytest.importorskip("lance")

    class AgentNamespace(LanceNamespace):
        ns_config = LanceNamespaceConfig(
            name="agent-tests",
            base_location=str(tmp_path),
        )
        people = LanceTable(schema=Person)
        summaries = LanceTable(schema=Summary)

    ns = AgentNamespace()
    ns.push()
    return ns


class FakePredictor:
    """Stands in for PredictRLM behind the _build_predictor seam."""

    def __init__(self, respond, output_field: str = "output"):
        self.respond = respond
        self.output_field = output_field
        self.calls: list[dict] = []

    async def acall(self, **inputs):
        self.calls.append(inputs)
        return SimpleNamespace(**{self.output_field: self.respond(inputs)})


def install_fake(monkeypatch, respond, output_field: str = "output"):
    """Monkeypatch the predictor seam; returns captured build kwargs + calls."""
    captured = {"builds": [], "predictors": []}

    def fake_build(signature, *, lm, sub_lm, skills, max_iterations, **extra):
        captured["builds"].append(
            {
                "signature": signature,
                "lm": lm,
                "sub_lm": sub_lm,
                "skills": skills,
                "max_iterations": max_iterations,
                "extra": extra,
            }
        )
        predictor = FakePredictor(respond, output_field=output_field)
        captured["predictors"].append(predictor)
        return predictor

    monkeypatch.setattr(agent_step_module, "_build_predictor", fake_build)
    return captured


class TestRegistration:
    def test_agent_step_registers_as_dag_node(self, lance_namespace):
        @agent_step(table=lance_namespace.summaries)
        async def summarize(person: Person) -> Summary:
            """Summarize one person."""
            ...

        assert isinstance(summarize, Node)
        assert summarize.node_type is NodeType.STEP
        assert summarize.name == "summarize"

    def test_sync_and_async_declarations_register(self, lance_namespace):
        @agent_step(table=lance_namespace.summaries)
        def sync_style(person: Person) -> Summary:
            """Sync declaration."""
            ...

        @agent_step(table=lance_namespace.summaries)
        async def async_style(person: Person) -> Summary:
            """Async declaration."""
            ...

        assert isinstance(sync_style, Node)
        assert isinstance(async_style, Node)

    def test_missing_return_annotation_rejected_at_decoration(self):
        with pytest.raises(AgentStepError, match="return annotation"):

            @agent_step
            async def no_return(person: Person):
                """Missing return annotation."""
                ...

    def test_non_model_return_annotation_rejected(self):
        with pytest.raises(AgentStepError, match="BaseModel"):

            @agent_step
            async def bad_return(person: Person) -> int:
                """Bad return annotation."""
                ...

    def test_composes_with_plain_steps_in_graph(self, lance_namespace, monkeypatch):
        install_fake(monkeypatch, lambda inputs: Summary(headline="h", person_count=1))

        @ava.step
        def load() -> Person:
            return Person(id=1, name="ada")

        @ava.step
        def other() -> int:
            return 7

        @agent_step(table=lance_namespace.summaries)
        async def summarize(person: Person) -> Summary:
            """Summarize."""
            ...

        @ava.workflow
        def flow():
            person = load()
            extra = other()
            summary = summarize(person)
            return (person & extra) >> summary

        wf = flow()
        node_names = {nf.node.name for nf in wf.nodes.values()}
        assert {"load", "other", "summarize"} <= node_names


class TestExecutionShapes:
    def test_scalar_map_end_to_end(self, lance_namespace, monkeypatch):
        captured = install_fake(
            monkeypatch,
            lambda inputs: Summary(
                headline=f"about {inputs['person'].name}", person_count=1
            ),
        )

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(Person(id=1, name="ada"))

        @agent_step(table=lance_namespace.summaries)
        async def summarize(person: Person) -> Summary:
            """Summarize one person."""
            ...

        @ava.workflow
        def flow():
            return summarize(load())

        result = flow().run(executor=LocalExecutor())

        assert isinstance(result, AppendResult)
        assert result.one() == Summary(headline="about ada", person_count=1)
        # The unwrapped argument arrived as a validated model.
        (call,) = captured["predictors"][0].calls
        assert call["person"] == Person(id=1, name="ada")
        # Persisted with provenance columns.
        stored = lance_namespace.summaries.scan().to_arrow()
        assert stored.num_rows == 1
        assert set(ROW_LINEAGE_COLUMNS) <= set(stored.column_names)
        assert lance_namespace.summaries.read_models() == [
            Summary(headline="about ada", person_count=1)
        ]

    def test_aggregate_list_param_receives_all_rows(self, lance_namespace, monkeypatch):
        captured = install_fake(
            monkeypatch,
            lambda inputs: Summary(
                headline="team", person_count=len(inputs["people"])
            ),
        )

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(
                [Person(id=1, name="ada"), Person(id=2, name="grace"), Person(id=3, name="kat")]
            )

        @agent_step(table=lance_namespace.summaries)
        async def merge(people: list[Person]) -> Summary:
            """Merge people into one summary."""
            ...

        @ava.workflow
        def flow():
            return merge(load())

        result = flow().run(executor=LocalExecutor())

        assert result.one().person_count == 3
        (call,) = captured["predictors"][0].calls
        assert [p.name for p in call["people"]] == ["ada", "grace", "kat"]

    def test_explode_list_return_appends_n_rows(self, lance_namespace, monkeypatch):
        install_fake(
            monkeypatch,
            lambda inputs: [
                Summary(headline="one", person_count=1),
                Summary(headline="two", person_count=1),
            ],
        )

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(Person(id=1, name="ada"))

        @agent_step(table=lance_namespace.summaries)
        async def split(person: Person) -> list[Summary]:
            """Split a person into several summaries."""
            ...

        @ava.workflow
        def flow():
            return split(load())

        result = flow().run(executor=LocalExecutor())

        models = result.to_models()
        assert [m.headline for m in models] == ["one", "two"]
        with pytest.raises(ValueError):
            result.one()
        assert len(lance_namespace.summaries.read_models()) == 2

    def test_implicit_rshift_chaining_unwraps_frame(self, lance_namespace, monkeypatch):
        captured = install_fake(
            monkeypatch,
            lambda inputs: Summary(
                headline=inputs["person"].name, person_count=1
            ),
        )

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(Person(id=9, name="implicit"))

        @agent_step(table=lance_namespace.summaries)
        async def summarize(person: Person) -> Summary:
            """Summarize."""
            ...

        @ava.workflow
        def flow():
            return load() >> summarize()

        result = flow().run(executor=LocalExecutor())

        assert result.one().headline == "implicit"
        (call,) = captured["predictors"][0].calls
        assert call["person"] == Person(id=9, name="implicit")

    def test_cardinality_mismatch_fails_at_boundary(self, lance_namespace, monkeypatch):
        install_fake(monkeypatch, lambda inputs: Summary(headline="x", person_count=1))

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append([Person(id=1, name="a"), Person(id=2, name="b")])

        @agent_step(table=lance_namespace.summaries)
        async def summarize(person: Person) -> Summary:
            """Summarize one person."""
            ...

        @ava.workflow
        def flow():
            return summarize(load())

        with pytest.raises(AgentStepError) as excinfo:
            flow().run(executor=LocalExecutor())

        message = str(excinfo.value)
        assert "summarize" in message
        assert "person" in message
        assert "2 rows" in message

    def test_non_tabular_value_for_model_param_rejected(
        self, lance_namespace, monkeypatch
    ):
        install_fake(monkeypatch, lambda inputs: Summary(headline="x", person_count=1))

        @ava.step
        def load() -> int:
            return 42

        @agent_step(table=lance_namespace.summaries)
        async def summarize(person: Person) -> Summary:
            """Summarize."""
            ...

        @ava.workflow
        def flow():
            return summarize(load())

        with pytest.raises(AgentStepError, match="int"):
            flow().run(executor=LocalExecutor())


class TestConfigPrecedence:
    def test_decorator_kwargs_beat_globals(self, lance_namespace, monkeypatch):
        captured = install_fake(
            monkeypatch, lambda inputs: Summary(headline="x", person_count=1)
        )
        configure_agent(
            lm="global-lm",
            sub_lm="global-sub",
            skills=["global-skill"],
            max_iterations=11,
        )

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(Person(id=1, name="ada"))

        @agent_step(table=lance_namespace.summaries, lm="step-lm", skills=["step-skill"])
        async def summarize(person: Person) -> Summary:
            """Summarize."""
            ...

        @ava.workflow
        def flow():
            return summarize(load())

        flow().run(executor=LocalExecutor())

        (build,) = captured["builds"]
        assert build["lm"] == "step-lm"  # decorator wins
        assert build["skills"] == ["step-skill"]  # decorator wins
        assert build["sub_lm"] == "global-sub"  # global fills the gap
        assert build["max_iterations"] == 11  # global fills the gap

    def test_extra_predictor_kwargs_forward_with_decorator_precedence(
        self, lance_namespace, monkeypatch
    ):
        captured = install_fake(
            monkeypatch, lambda inputs: Summary(headline="x", person_count=1)
        )
        configure_agent(verbose=False, max_llm_calls=10)

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(Person(id=1, name="ada"))

        @agent_step(table=lance_namespace.summaries, verbose=True, debug=True)
        async def summarize(person: Person) -> Summary:
            """Summarize."""
            ...

        @ava.workflow
        def flow():
            return summarize(load())

        flow().run(executor=LocalExecutor())

        (build,) = captured["builds"]
        assert build["extra"] == {
            "verbose": True,  # decorator wins over configure_agent
            "debug": True,  # decorator-only extra forwarded
            "max_llm_calls": 10,  # configure_agent extra fills the gap
        }

    def test_reserved_predictor_kwargs_rejected_on_configure(self):
        # `signature` and `table` are agent-step options, not PredictRLM kwargs;
        # configure_agent must refuse to smuggle them into every predictor.
        with pytest.raises(TypeError, match="reserved"):
            configure_agent(signature="nope")
        with pytest.raises(TypeError, match="reserved"):
            configure_agent(table="nope")

    def test_namespace_fallback_derives_table(self, lance_namespace, monkeypatch):
        install_fake(monkeypatch, lambda inputs: Summary(headline="derived", person_count=1))
        configure_agent(namespace=lance_namespace)

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(Person(id=1, name="ada"))

        @agent_step
        async def auto_summarize(person: Person) -> Summary:
            """Summarize."""
            ...

        @ava.workflow
        def flow():
            return auto_summarize(load())

        result = flow().run(executor=LocalExecutor())

        assert result.one().headline == "derived"
        derived = getattr(lance_namespace, "auto_summarize")
        assert isinstance(derived, LanceTable)
        assert derived.row_model is Summary
        assert derived.read_models() == [Summary(headline="derived", person_count=1)]

    def test_no_table_and_no_namespace_is_a_config_error(
        self, lance_namespace, monkeypatch
    ):
        install_fake(monkeypatch, lambda inputs: Summary(headline="x", person_count=1))

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(Person(id=1, name="ada"))

        @agent_step
        async def summarize(person: Person) -> Summary:
            """Summarize."""
            ...

        @ava.workflow
        def flow():
            return summarize(load())

        with pytest.raises(AgentStepError) as excinfo:
            flow().run(executor=LocalExecutor())
        message = str(excinfo.value)
        assert "table=" in message
        assert "configure_agent" in message

    def test_table_model_mismatch_rejected(self, lance_namespace, monkeypatch):
        install_fake(monkeypatch, lambda inputs: Summary(headline="x", person_count=1))

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(Person(id=1, name="ada"))

        @agent_step(table=lance_namespace.people)  # Person table, Summary output
        async def summarize(person: Person) -> Summary:
            """Summarize."""
            ...

        @ava.workflow
        def flow():
            return summarize(load())

        with pytest.raises(AgentStepError, match="row model"):
            flow().run(executor=LocalExecutor())


class TestSignatureBridge:
    def test_existing_signature_executes_and_stores(self, lance_namespace, monkeypatch):
        dspy = pytest.importorskip("dspy")

        class HandWritten(dspy.Signature):
            """Hand-written signature."""

            person: Person = dspy.InputField()
            summary: Summary = dspy.OutputField()

        captured = install_fake(
            monkeypatch,
            lambda inputs: Summary(headline="bridged", person_count=1),
            output_field="summary",
        )

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(Person(id=1, name="ada"))

        @agent_step(table=lance_namespace.summaries, signature=HandWritten)
        async def summarize(person: Person) -> Summary:
            """Ignored: the signature defines the contract."""
            ...

        @ava.workflow
        def flow():
            return summarize(load())

        result = flow().run(executor=LocalExecutor())

        assert result.one().headline == "bridged"
        (build,) = captured["builds"]
        assert build["signature"] is HandWritten
        (call,) = captured["predictors"][0].calls
        assert call["person"] == Person(id=1, name="ada")

    def test_multi_output_signature_rejected(self, lance_namespace, monkeypatch):
        dspy = pytest.importorskip("dspy")

        class TwoOutputs(dspy.Signature):
            """Two outputs."""

            person: Person = dspy.InputField()
            summary: Summary = dspy.OutputField()
            note: str = dspy.OutputField()

        install_fake(monkeypatch, lambda inputs: Summary(headline="x", person_count=1))

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(Person(id=1, name="ada"))

        @agent_step(table=lance_namespace.summaries, signature=TwoOutputs)
        async def summarize(person: Person) -> Summary:
            """Doc."""
            ...

        @ava.workflow
        def flow():
            return summarize(load())

        with pytest.raises(AgentStepError, match="exactly one output field"):
            flow().run(executor=LocalExecutor())


class TestInputAndPersistence:
    def test_ava_input_flows_into_agent_kwargs(self, lance_namespace, monkeypatch):
        captured = install_fake(
            monkeypatch,
            lambda inputs: Summary(headline=inputs["topic"], person_count=0),
        )

        class RunInput(ava.BaseInput):
            topic: str

        @agent_step(table=lance_namespace.summaries)
        async def research(topic: str) -> Summary:
            """Research a topic."""
            ...

        @ava.workflow(input=RunInput)
        def flow():
            return research(topic=ava.input.topic)

        result = flow().run(executor=LocalExecutor(), input={"topic": "icebergs"})

        assert result.one().headline == "icebergs"
        (call,) = captured["predictors"][0].calls
        assert call["topic"] == "icebergs"

    def test_persist_at_produce_survives_downstream_failure(
        self, lance_namespace, monkeypatch
    ):
        install_fake(
            monkeypatch, lambda inputs: Summary(headline="durable", person_count=1)
        )

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(Person(id=1, name="ada"))

        @agent_step(table=lance_namespace.summaries)
        async def summarize(person: Person) -> Summary:
            """Summarize."""
            ...

        @ava.step
        def explode_downstream(summary):
            raise RuntimeError("downstream boom")

        @ava.workflow
        def flow():
            return explode_downstream(summarize(load()))

        with pytest.raises(RuntimeError, match="downstream boom"):
            flow().run(executor=LocalExecutor())

        assert lance_namespace.summaries.read_models() == [
            Summary(headline="durable", person_count=1)
        ]

    def test_predictor_failure_surfaces_signature_and_input_types(
        self, lance_namespace, monkeypatch
    ):
        def boom(inputs):
            raise RuntimeError("model exploded")

        install_fake(monkeypatch, boom)

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(Person(id=1, name="ada"))

        @agent_step(table=lance_namespace.summaries)
        async def summarize(person: Person) -> Summary:
            """Summarize."""
            ...

        @ava.workflow
        def flow():
            return summarize(load())

        with pytest.raises(AgentStepExecutionError) as excinfo:
            flow().run(executor=LocalExecutor())

        message = str(excinfo.value)
        assert "summarize" in message  # step name
        assert "SummarizeSignature" in message  # generated signature name
        assert "person" in message  # input field
        assert "Person" in message  # offending input type

    def test_invalid_output_type_surfaces_execution_error(
        self, lance_namespace, monkeypatch
    ):
        install_fake(monkeypatch, lambda inputs: "not a model")

        @ava.step
        def load(*, people=lance_namespace.people) -> AppendResult:
            return people.append(Person(id=1, name="ada"))

        @agent_step(table=lance_namespace.summaries)
        async def summarize(person: Person) -> Summary:
            """Summarize."""
            ...

        @ava.workflow
        def flow():
            return summarize(load())

        with pytest.raises(AgentStepExecutionError, match="Summary"):
            flow().run(executor=LocalExecutor())
