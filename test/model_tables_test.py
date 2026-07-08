from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import dataframely as dy
import polars as pl
import pytest
from pydantic import BaseModel

from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable
from avalanche.lance import LanceNamespace, LanceNamespaceConfig, LanceTable
from avalanche.lineage import ROW_LINEAGE_COLUMNS


class Address(BaseModel):
    city: str
    zip_code: str | None = None


class Person(BaseModel):
    id: int
    name: str
    address: Address
    tags: list[str]


class OtherModel(BaseModel):
    id: int


class FrameSchema(dy.Schema):
    id = dy.Int64(nullable=False)
    name = dy.String(nullable=False)


@dataclass(frozen=True)
class BackendCase:
    name: str
    table_cls: type
    namespace_cls: type
    namespace_config_cls: type
    namespace_kwargs: Callable[[str], dict[str, Any]]
    requires: tuple[str, ...] = ()


BACKENDS = [
    BackendCase(
        name="iceberg",
        table_cls=IcebergTable,
        namespace_cls=IcebergNs,
        namespace_config_cls=IcebergNsConfig,
        namespace_kwargs=lambda tmpdir: {
            "catalog": "model-catalog",
            "load_catalog_props": {"type": "sql", "uri": "sqlite:///:memory:"},
        },
    ),
    BackendCase(
        name="lance",
        table_cls=LanceTable,
        namespace_cls=LanceNamespace,
        namespace_config_cls=LanceNamespaceConfig,
        namespace_kwargs=lambda tmpdir: {},
        requires=("lance",),
    ),
]


def _skip_missing_backend(case: BackendCase) -> None:
    for module_name in case.requires:
        pytest.importorskip(module_name)


@pytest.fixture(params=BACKENDS, ids=lambda case: case.name)
def backend(request: pytest.FixtureRequest) -> BackendCase:
    case = request.param
    _skip_missing_backend(case)
    return case


@pytest.fixture
def namespace(backend: BackendCase, tmp_path):
    class ModelNamespace(backend.namespace_cls):
        ns_config = backend.namespace_config_cls(
            name=f"model-{backend.name}",
            base_location=str(tmp_path),
        )
        people = backend.table_cls(schema=Person)
        frame_rows = backend.table_cls(schema=FrameSchema)

    ns = ModelNamespace(**backend.namespace_kwargs(str(tmp_path)))
    ns.push()
    return ns


def _person(row_id: int, name: str, city: str, zip_code: str | None, tags: list[str]) -> Person:
    return Person(
        id=row_id,
        name=name,
        address=Address(city=city, zip_code=zip_code),
        tags=tags,
    )


def _people() -> list[Person]:
    return [
        _person(1, "ada", "Toronto", "M5V", ["founder", "math"]),
        _person(2, "grace", "Arlington", None, ["compiler"]),
        _person(3, "katherine", "White Sulphur Springs", "24986", []),
    ]


def test_model_table_appends_single_and_list_then_reads_models(namespace):
    people = _people()

    namespace.people.append(people[0])
    namespace.people.append(people[1:])

    models = namespace.people.read_models()
    assert all(isinstance(model, Person) for model in models)
    assert sorted(models, key=lambda person: person.id) == sorted(
        people, key=lambda person: person.id
    )


def test_nested_struct_and_list_survive_backend_round_trip(namespace):
    person = _person(10, "dorothy", "Kansas City", None, ["navigation", "radar"])

    namespace.people.append(person)

    restored = namespace.people.read_models()
    assert restored == [person]
    assert restored[0].address == Address(city="Kansas City", zip_code=None)
    assert restored[0].tags == ["navigation", "radar"]


def test_model_table_stores_row_lineage_columns(namespace):
    namespace.people.append(_people()[0])

    column_names = namespace.people.scan().to_arrow().schema.names
    for column in ROW_LINEAGE_COLUMNS:
        assert column in column_names


def test_model_append_result_exposes_typed_rows(namespace):
    person = _people()[0]

    result = namespace.people.append(person)

    assert result.row_model is Person
    assert result.to_models() == [person]
    assert result.one() == person


def test_wrong_model_instance_append_raises_type_error(namespace):
    with pytest.raises(TypeError, match="Person"):
        namespace.people.append(OtherModel(id=1))


def test_empty_model_list_append_raises_value_error(namespace):
    with pytest.raises(ValueError, match="zero rows"):
        namespace.people.append([])


def test_model_append_on_dataframely_table_raises_type_error(namespace):
    with pytest.raises(TypeError, match="pydantic model schema"):
        namespace.frame_rows.append(_people()[0])


def test_read_models_on_dataframely_table_raises_type_error(namespace):
    with pytest.raises(TypeError, match="pydantic model schema"):
        namespace.frame_rows.read_models()


def test_dataframely_table_still_accepts_polars_dataframe(namespace):
    rows = pl.DataFrame({"id": [1, 2], "name": ["one", "two"]})

    result = namespace.frame_rows.append(rows)
    stored = namespace.frame_rows.read().select(["id", "name"])

    assert result.to_polars().select(["id", "name"]).to_dicts() == rows.to_dicts()
    assert stored.to_dicts() == rows.to_dicts()
