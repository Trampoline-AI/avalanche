"""``@ava.agent_step`` — agent-backed workflow steps.

An agent step is an ordinary Avalanche DAG node: it registers through the same
``Node`` path as ``@ava.step`` and participates in ``>>`` / ``&`` chaining,
retries, async execution, and TUI rendering identically. Only the executor
differs: instead of running the function body, Avalanche calls a generated
DSPy signature through predict-rlm's ``PredictRLM``, appends the returned
model to the step's table, and returns the typed ``AppendResult``.

dspy and predict_rlm are imported lazily — importing this module (or
decorating functions with ``agent_step``) pulls in neither.
"""

from __future__ import annotations

import inspect
from functools import update_wrapper
from typing import (
    Annotated,
    Any,
    Sequence,
    get_args,
    get_origin,
    get_type_hints,
)

import polars as pl
import pyarrow as pa
from pydantic import BaseModel

from ..dag import Node, NodeType
from ..model_frame import arrow_to_models
from ..storage import Namespace, Table
from ..types import AppendResult
from .config import AgentConfig, get_agent_config
from .signature import generate_signature

OUTPUT_FIELD_NAME = "output"


class AgentStepError(RuntimeError):
    """Configuration or boundary contract violation on an agent step."""


class AgentStepExecutionError(RuntimeError):
    """An agent step's predictor call or output validation failed."""


def agent_step(
    fn: Any = None,
    *,
    lm: Any = None,
    sub_lm: Any = None,
    skills: Sequence[Any] | None = None,
    max_iterations: int | None = None,
    table: Table | None = None,
    signature: Any = None,
) -> Node | Any:
    """Register a typed, docstring-annotated function as an agent-backed step.

    Usage::

        @ava.agent_step(skills=[ava.skills.pdf])
        async def audit_rfp(
            documents: Annotated[list[File], ava.Desc("RFP documents")],
        ) -> RfpAudit:
            \"\"\"Read the RFP package and identify submission requirements.\"\"\"
            ...

    The function body never runs. Parameters become the agent's input fields,
    the docstring becomes its instructions, and the return annotation defines
    both the output model and the destination table schema. Kwargs ``lm``,
    ``sub_lm``, ``skills``, and ``max_iterations`` override
    ``ava.configure_agent`` globals key by key. ``table=`` pins the
    destination table; otherwise one is derived in the
    ``configure_agent(namespace=...)`` namespace, named after the function.
    ``signature=`` bypasses generation for existing hand-written DSPy
    signatures.
    """

    def decorator(user_fn: Any) -> Node:
        return _make_agent_node(
            user_fn,
            lm=lm,
            sub_lm=sub_lm,
            skills=skills,
            max_iterations=max_iterations,
            table=table,
            signature=signature,
        )

    if fn is not None:
        return decorator(fn)
    return decorator


def _build_predictor(
    signature: Any,
    *,
    lm: Any,
    sub_lm: Any,
    skills: Sequence[Any] | None,
    max_iterations: int | None,
) -> Any:
    """Build the predict-rlm predictor for one agent-step execution.

    Single seam between Avalanche and predict-rlm; tests monkeypatch this
    function so no test ever calls a real LM.
    """
    from predict_rlm import PredictRLM

    kwargs: dict[str, Any] = {}
    if lm is not None:
        kwargs["lm"] = lm
    if sub_lm is not None:
        kwargs["sub_lm"] = sub_lm
    if skills is not None:
        kwargs["skills"] = list(skills)
    if max_iterations is not None:
        kwargs["max_iterations"] = max_iterations
    return PredictRLM(signature, **kwargs)


class _AgentStepSpec:
    """Per-step immutable definition plus lazily-resolved execution state."""

    def __init__(
        self,
        user_fn: Any,
        *,
        lm: Any,
        sub_lm: Any,
        skills: Sequence[Any] | None,
        max_iterations: int | None,
        table: Table | None,
        signature: Any,
    ) -> None:
        self.user_fn = user_fn
        self.step_name = user_fn.__name__
        self.lm = lm
        self.sub_lm = sub_lm
        self.skills = skills
        self.max_iterations = max_iterations
        self.table = table
        self.bridge_signature = signature

        self.fn_signature = inspect.signature(user_fn)
        self.hints = get_type_hints(user_fn, include_extras=True)
        self.runtime_params = _runtime_param_names(self.hints)

        return_annotation = self.hints.get("return")
        if signature is None:
            self.output_model, self.explode = _parse_output_annotation(
                self.step_name, return_annotation, required=True
            )
        else:
            # Bridge: the signature defines the output; validate a return
            # annotation eagerly when present, reconcile at first execution.
            self.output_model, self.explode = _parse_output_annotation(
                self.step_name, return_annotation, required=False
            )

        # Lazily resolved (first execution).
        self._dspy_signature: Any = None
        self._input_annotations: dict[str, Any] | None = None
        self._resolved_table: Table | None = None

    # -- signature -----------------------------------------------------------

    def resolve_signature(self) -> tuple[Any, dict[str, Any]]:
        """Return (dspy signature, input-field annotations), resolving once."""
        if self._dspy_signature is not None:
            return self._dspy_signature, self._input_annotations or {}

        if self.bridge_signature is not None:
            dspy_signature = self.bridge_signature
            self._adopt_bridge_output(dspy_signature)
        else:
            dspy_signature = generate_signature(
                self.user_fn,
                output_field_name=OUTPUT_FIELD_NAME,
                skip_params=self.runtime_params,
            )

        input_annotations = {
            name: field.annotation
            for name, field in dspy_signature.input_fields.items()
        }
        self._dspy_signature = dspy_signature
        self._input_annotations = input_annotations
        return dspy_signature, input_annotations

    def _adopt_bridge_output(self, dspy_signature: Any) -> None:
        output_fields = getattr(dspy_signature, "output_fields", None)
        if not isinstance(output_fields, dict) or len(output_fields) != 1:
            count = len(output_fields) if isinstance(output_fields, dict) else "no"
            raise AgentStepError(
                f"agent step '{self.step_name}': signature= bridge requires a DSPy "
                f"signature with exactly one output field; "
                f"{_describe_signature(dspy_signature)} has {count} output fields."
            )

        (output_name,) = output_fields
        annotation = output_fields[output_name].annotation
        model, explode = _parse_output_annotation(
            self.step_name, annotation, required=True
        )
        if self.output_model is not None and (
            model is not self.output_model or explode is not self.explode
        ):
            raise AgentStepError(
                f"agent step '{self.step_name}': return annotation "
                f"{self.hints.get('return')!r} does not match the signature= output "
                f"field '{output_name}' annotation {annotation!r}."
            )
        self.output_model = model
        self.explode = explode
        self.output_field_name = output_name

    @property
    def resolved_output_field_name(self) -> str:
        return getattr(self, "output_field_name", OUTPUT_FIELD_NAME)

    # -- table ----------------------------------------------------------------

    def resolve_table(self, config: AgentConfig) -> Table:
        if self._resolved_table is not None:
            return self._resolved_table

        assert self.output_model is not None
        if self.table is not None:
            table = self.table
            if table.row_model is None:
                raise AgentStepError(
                    f"agent step '{self.step_name}': table= must be declared from a "
                    f"pydantic model schema so it can store {self.output_model.__name__} "
                    "rows and return a typed AppendResult."
                )
            if table.row_model is not self.output_model:
                raise AgentStepError(
                    f"agent step '{self.step_name}': table= row model "
                    f"{table.row_model.__name__} does not match the step output model "
                    f"{self.output_model.__name__}."
                )
            if table._ns is None:
                raise AgentStepError(
                    f"agent step '{self.step_name}': table= is not bound to a "
                    "namespace. Declare it on a namespace and call namespace.push() "
                    "before running the workflow."
                )
        elif config.namespace is not None:
            table = _derive_table(config.namespace, self.step_name, self.output_model)
        else:
            raise AgentStepError(
                f"agent step '{self.step_name}' has no destination table. Pass "
                "@ava.agent_step(table=...) or set a default namespace with "
                "ava.configure_agent(namespace=...)."
            )

        self._resolved_table = table
        return table


def _make_agent_node(
    user_fn: Any,
    *,
    lm: Any,
    sub_lm: Any,
    skills: Sequence[Any] | None,
    max_iterations: int | None,
    table: Table | None,
    signature: Any,
) -> Node:
    spec = _AgentStepSpec(
        user_fn,
        lm=lm,
        sub_lm=sub_lm,
        skills=skills,
        max_iterations=max_iterations,
        table=table,
        signature=signature,
    )

    async def wrapper(*args: Any, **kwargs: Any) -> AppendResult:
        return await _execute_agent_step(spec, args, kwargs)

    update_wrapper(wrapper, user_fn)
    wrapper.__signature__ = spec.fn_signature  # type: ignore[attr-defined]
    wrapper.__agent_step__ = spec  # type: ignore[attr-defined]

    return Node(wrapper, NodeType.STEP, num_returns=1)


async def _execute_agent_step(
    spec: _AgentStepSpec, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> AppendResult:
    bound = spec.fn_signature.bind(*args, **kwargs)

    dspy_signature, input_annotations = spec.resolve_signature()

    inputs: dict[str, Any] = {}
    for name, value in bound.arguments.items():
        if name in spec.runtime_params:
            continue
        if name not in input_annotations:
            raise AgentStepError(
                f"agent step '{spec.step_name}': argument '{name}' has no matching "
                f"input field on {_describe_signature(dspy_signature)}."
            )
        inputs[name] = _unwrap_argument(
            spec.step_name, name, input_annotations[name], value
        )

    missing = sorted(set(input_annotations) - set(inputs))
    if missing:
        raise AgentStepError(
            f"agent step '{spec.step_name}': missing values for input fields "
            f"{missing} of {_describe_signature(dspy_signature)}."
        )

    config = get_agent_config()
    predictor = _build_predictor(
        dspy_signature,
        lm=spec.lm if spec.lm is not None else config.lm,
        sub_lm=spec.sub_lm if spec.sub_lm is not None else config.sub_lm,
        skills=spec.skills if spec.skills is not None else config.skills,
        max_iterations=(
            spec.max_iterations
            if spec.max_iterations is not None
            else config.max_iterations
        ),
    )

    try:
        prediction = await predictor.acall(**inputs)
    except Exception as exc:
        raise AgentStepExecutionError(
            _execution_error_message(spec, dspy_signature, inputs, str(exc))
        ) from exc

    output_value = _extract_output(spec, dspy_signature, inputs, prediction)
    resolved_table = spec.resolve_table(config)
    return resolved_table.append(output_value)


# -- argument unwrapping -------------------------------------------------------


def _unwrap_argument(step_name: str, param: str, annotation: Any, value: Any) -> Any:
    """Unwrap an upstream value by the parameter's declared annotation.

    ``Model`` parameters assert exactly-one-row cardinality; ``list[Model]``
    parameters receive every row. Mismatches fail here, at the step boundary.
    """
    base = _strip_annotated(annotation)

    if _is_model(base):
        if isinstance(value, base):
            return value
        models = _rows_to_models(value, base)
        if models is None:
            raise AgentStepError(
                f"agent step '{step_name}': parameter '{param}' is annotated "
                f"{base.__name__} but received {type(value).__name__}."
            )
        if len(models) != 1:
            raise AgentStepError(
                f"agent step '{step_name}': parameter '{param}' is annotated "
                f"{base.__name__} and expects exactly one row; the upstream result "
                f"has {len(models)} rows. Annotate it list[{base.__name__}] to accept "
                "them all."
            )
        return models[0]

    element = _list_model_element(base)
    if element is not None:
        if isinstance(value, list) and all(isinstance(item, element) for item in value):
            return value
        models = _rows_to_models(value, element)
        if models is None:
            raise AgentStepError(
                f"agent step '{step_name}': parameter '{param}' is annotated "
                f"list[{element.__name__}] but received {type(value).__name__}."
            )
        return models

    if isinstance(value, (AppendResult, pl.DataFrame, pa.Table, pa.RecordBatch)):
        raise AgentStepError(
            f"agent step '{step_name}': parameter '{param}' received tabular upstream "
            f"data ({type(value).__name__}) but is annotated {annotation!r}. Annotate "
            "it with the row model (Model or list[Model]) to unwrap it."
        )
    return value


def _rows_to_models(value: Any, model: type[BaseModel]) -> list[BaseModel] | None:
    if isinstance(value, AppendResult):
        return arrow_to_models(value.to_arrow(), model)
    if isinstance(value, pl.DataFrame):
        return arrow_to_models(value, model)
    if isinstance(value, pa.RecordBatch):
        return arrow_to_models(pa.Table.from_batches([value]), model)
    if isinstance(value, pa.Table):
        return arrow_to_models(value, model)
    return None


# -- output handling ------------------------------------------------------------


def _extract_output(
    spec: _AgentStepSpec, dspy_signature: Any, inputs: dict[str, Any], prediction: Any
) -> BaseModel | list[BaseModel]:
    output_name = spec.resolved_output_field_name
    model = spec.output_model
    assert model is not None

    try:
        value = getattr(prediction, output_name)
    except AttributeError:
        raise AgentStepExecutionError(
            _execution_error_message(
                spec,
                dspy_signature,
                inputs,
                f"prediction has no '{output_name}' field",
            )
        ) from None

    def coerce(item: Any) -> BaseModel:
        if isinstance(item, model):
            return item
        if isinstance(item, dict):
            return model.model_validate(item)
        raise AgentStepExecutionError(
            _execution_error_message(
                spec,
                dspy_signature,
                inputs,
                f"expected {model.__name__} output, got {type(item).__name__}",
            )
        )

    if spec.explode:
        if not isinstance(value, list):
            raise AgentStepExecutionError(
                _execution_error_message(
                    spec,
                    dspy_signature,
                    inputs,
                    f"expected list[{model.__name__}] output, "
                    f"got {type(value).__name__}",
                )
            )
        if not value:
            raise AgentStepExecutionError(
                _execution_error_message(
                    spec,
                    dspy_signature,
                    inputs,
                    f"expected at least one {model.__name__} row, got an empty list",
                )
            )
        return [coerce(item) for item in value]

    return coerce(value)


# -- table derivation -----------------------------------------------------------


def _derive_table(
    namespace: Namespace, step_name: str, output_model: type[BaseModel]
) -> Table:
    existing = getattr(namespace, step_name, None)
    if isinstance(existing, Table):
        if existing.row_model is not output_model:
            raise AgentStepError(
                f"agent step '{step_name}': namespace '{namespace.name}' already has "
                f"a table named '{step_name}' with row model "
                f"{getattr(existing.row_model, '__name__', None)!r}, which does not "
                f"match the step output model {output_model.__name__}."
            )
        table = existing
    else:
        if existing is not None:
            raise AgentStepError(
                f"agent step '{step_name}': namespace '{namespace.name}' attribute "
                f"'{step_name}' is not a table; cannot auto-derive a destination."
            )
        table = _new_table_for_namespace(namespace, step_name, output_model)
        table._ns = namespace
        table._table_name = step_name
        setattr(namespace, step_name, table)

    _bind_backend_table(namespace, table, step_name)
    return table


def _new_table_for_namespace(
    namespace: Namespace, step_name: str, output_model: type[BaseModel]
) -> Table:
    from ..iceberg.namespace import IcebergNamespace
    from ..iceberg.table import IcebergTable
    from ..lance.namespace import LanceNamespace
    from ..lance.table import LanceTable

    if isinstance(namespace, IcebergNamespace):
        return IcebergTable(schema=output_model)
    if isinstance(namespace, LanceNamespace):
        return LanceTable(schema=output_model)
    raise AgentStepError(
        f"agent step '{step_name}': cannot auto-derive a table in "
        f"{type(namespace).__name__}; pass @ava.agent_step(table=...) instead."
    )


def _bind_backend_table(namespace: Namespace, table: Table, step_name: str) -> None:
    from ..iceberg.namespace import IcebergNamespace
    from ..lance.namespace import LanceNamespace

    if isinstance(namespace, IcebergNamespace):
        if getattr(table, "_table", None) is None:
            if (namespace.name,) not in namespace.catalog.list_namespaces():
                namespace.catalog.create_namespace(namespace.name)
            namespace._push_table(table)
        return
    if isinstance(namespace, LanceNamespace):
        from pathlib import Path

        Path(table.location).mkdir(parents=True, exist_ok=True)
        return
    raise AgentStepError(
        f"agent step '{step_name}': unsupported namespace type "
        f"{type(namespace).__name__}."
    )


# -- annotation helpers ----------------------------------------------------------


def _runtime_param_names(hints: dict[str, Any]) -> set[str]:
    from ..runtime import BaseContext, BaseInput

    names = set()
    for name, annotation in hints.items():
        if name == "return":
            continue
        base = _strip_annotated(annotation)
        if isinstance(base, type) and issubclass(base, (BaseInput, BaseContext)):
            names.add(name)
    return names


def _parse_output_annotation(
    step_name: str, annotation: Any, *, required: bool
) -> tuple[type[BaseModel] | None, bool]:
    if annotation is None:
        if required:
            raise AgentStepError(
                f"agent step '{step_name}' needs a return annotation: a pydantic "
                "BaseModel subclass or list[Model] defining its output and table "
                "schema."
            )
        return None, False

    base = _strip_annotated(annotation)
    if _is_model(base):
        return base, False
    element = _list_model_element(base)
    if element is not None:
        return element, True
    raise AgentStepError(
        f"agent step '{step_name}' return annotation {annotation!r} is not a "
        "pydantic BaseModel subclass or list[Model]."
    )


def _strip_annotated(annotation: Any) -> Any:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _list_model_element(annotation: Any) -> type[BaseModel] | None:
    if get_origin(annotation) is not list:
        return None
    args = get_args(annotation)
    if len(args) == 1 and _is_model(_strip_annotated(args[0])):
        return _strip_annotated(args[0])
    return None


# -- error formatting -------------------------------------------------------------


def _describe_signature(dspy_signature: Any) -> str:
    name = getattr(dspy_signature, "__name__", type(dspy_signature).__name__)
    try:
        input_fields = {
            field_name: getattr(field.annotation, "__name__", repr(field.annotation))
            for field_name, field in dspy_signature.input_fields.items()
        }
        output_fields = {
            field_name: getattr(field.annotation, "__name__", repr(field.annotation))
            for field_name, field in dspy_signature.output_fields.items()
        }
    except Exception:
        return f"signature {name}"
    rendered_inputs = ", ".join(f"{k}: {v}" for k, v in input_fields.items())
    rendered_outputs = ", ".join(f"{k}: {v}" for k, v in output_fields.items())
    return f"signature {name}({rendered_inputs}) -> ({rendered_outputs})"


def _execution_error_message(
    spec: _AgentStepSpec,
    dspy_signature: Any,
    inputs: dict[str, Any],
    reason: str,
) -> str:
    input_types = {name: type(value).__name__ for name, value in inputs.items()}
    return (
        f"agent step '{spec.step_name}' failed: {reason}. "
        f"Generated {_describe_signature(dspy_signature)}; "
        f"input types: {input_types}."
    )
