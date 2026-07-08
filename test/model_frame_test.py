"""Tests for pydantic model frame conversion."""

from __future__ import annotations

from datetime import date, datetime
from enum import IntEnum, StrEnum
from typing import Annotated, Any

import polars as pl
import pyarrow as pa
import pytest
from pydantic import BaseModel, Field
from pydantic_core import core_schema

from avalanche import Json
from avalanche.model_frame import (
    UnsupportedModelFieldError,
    arrow_to_models,
    model_to_arrow_schema,
    models_to_arrow,
)


class ScalarModel(BaseModel):
    name: str
    count: int
    ratio: float
    active: bool
    created_at: datetime
    starts_on: date
    content: bytes


class NestedModel(BaseModel):
    label: str
    value: int


class NestedContainer(BaseModel):
    nested: NestedModel


class ListContainer(BaseModel):
    tags: list[str]
    nested_items: list[NestedModel]


class OptionalModel(BaseModel):
    required: int
    maybe: int | None


class Status(StrEnum):
    READY = "ready"
    DONE = "done"


class Rank(IntEnum):
    LOW = 1
    HIGH = 2


class EnumModel(BaseModel):
    status: Status
    rank: Rank


def test_scalar_mapping_uses_expected_arrow_types():
    schema = model_to_arrow_schema(ScalarModel)

    expected = {
        "name": pa.string(),
        "count": pa.int64(),
        "ratio": pa.float64(),
        "active": pa.bool_(),
        "created_at": pa.timestamp("us"),
        "starts_on": pa.date32(),
        "content": pa.binary(),
    }
    for name, arrow_type in expected.items():
        field = schema.field(name)
        assert field.type == arrow_type
        assert field.nullable is False

    empty = models_to_arrow([], ScalarModel)
    assert empty.schema == schema
    assert empty.num_rows == 0


def test_nested_model_maps_to_struct_with_child_types():
    schema = model_to_arrow_schema(NestedContainer)
    nested = schema.field("nested")

    assert nested.type == pa.struct(
        [
            pa.field("label", pa.string(), nullable=False),
            pa.field("value", pa.int64(), nullable=False),
        ]
    )


def test_list_fields_map_to_arrow_lists():
    schema = model_to_arrow_schema(ListContainer)

    assert schema.field("tags").type == pa.list_(pa.string())
    assert schema.field("nested_items").type == pa.list_(
        pa.struct(
            [
                pa.field("label", pa.string(), nullable=False),
                pa.field("value", pa.int64(), nullable=False),
            ]
        )
    )


def test_optional_fields_are_nullable_and_round_trip_none():
    schema = model_to_arrow_schema(OptionalModel)

    assert schema.field("required").type == pa.int64()
    assert schema.field("required").nullable is False
    assert schema.field("maybe").type == pa.int64()
    assert schema.field("maybe").nullable is True

    rows = [OptionalModel(required=1, maybe=None)]
    assert arrow_to_models(models_to_arrow(rows, OptionalModel), OptionalModel) == rows


def test_enums_map_to_scalars_and_round_trip_to_members():
    schema = model_to_arrow_schema(EnumModel)

    assert schema.field("status").type == pa.string()
    assert schema.field("rank").type == pa.int64()

    rows = [EnumModel(status=Status.READY, rank=Rank.HIGH)]
    restored = arrow_to_models(models_to_arrow(rows, EnumModel), EnumModel)

    assert restored == rows
    assert restored[0].status is Status.READY
    assert restored[0].rank is Rank.HIGH


def test_field_descriptions_are_arrow_metadata_at_every_level():
    class DescribedChild(BaseModel):
        detail: str = Field(description="child detail")

    class DescribedParent(BaseModel):
        title: str = Field(description="top detail")
        child: DescribedChild

    schema = model_to_arrow_schema(DescribedParent)

    assert schema.field("title").metadata[b"description"] == b"top detail"
    child_metadata = schema.field("child").type.field("detail").metadata
    assert child_metadata[b"description"] == b"child detail"


def test_models_round_trip_with_nested_lists_optional_enums_and_datetimes():
    class Comment(BaseModel):
        body: str
        created_at: datetime

    class Profile(BaseModel):
        handle: str
        visits: int

    class Record(BaseModel):
        name: str
        when: datetime
        maybe: int | None
        status: Status
        rank: Rank
        profile: Profile
        tags: list[str]
        comments: list[Comment]

    rows = [
        Record(
            name="first",
            when=datetime(2026, 1, 2, 3, 4, 5, 6),
            maybe=None,
            status=Status.READY,
            rank=Rank.LOW,
            profile=Profile(handle="alpha", visits=4),
            tags=["a", "b"],
            comments=[Comment(body="ok", created_at=datetime(2026, 1, 3, 4, 5, 6))],
        ),
        Record(
            name="second",
            when=datetime(2026, 2, 3, 4, 5, 6),
            maybe=9,
            status=Status.DONE,
            rank=Rank.HIGH,
            profile=Profile(handle="beta", visits=5),
            tags=[],
            comments=[],
        ),
    ]

    assert arrow_to_models(models_to_arrow(rows, Record), Record) == rows


def test_json_marker_stores_json_string_and_round_trips_dict():
    class JsonModel(BaseModel):
        payload: Annotated[dict[str, Any], Json]
        payload_instance: Annotated[dict[str, Any], Json()]

    row = JsonModel(
        payload={"name": "ava", "count": 3},
        payload_instance={"items": [1, {"ok": True}]},
    )
    table = models_to_arrow([row], JsonModel)

    assert table.schema.field("payload").type == pa.string()
    assert table.schema.field("payload_instance").type == pa.string()

    stored = table.column("payload").to_pylist()[0]
    assert isinstance(stored, str)
    assert stored == '{"name": "ava", "count": 3}'
    assert arrow_to_models(table, JsonModel) == [row]


def test_json_marker_maps_non_optional_union_to_string_and_round_trips():
    class JsonUnionModel(BaseModel):
        v: Annotated[int | str, Json]

    schema = model_to_arrow_schema(JsonUnionModel)
    field = schema.field("v")

    assert field.type == pa.string()
    assert field.nullable is False

    rows = [JsonUnionModel(v=7), JsonUnionModel(v="seven")]
    restored = arrow_to_models(models_to_arrow(rows, JsonUnionModel), JsonUnionModel)

    assert restored == rows
    assert restored[0].v == 7
    assert isinstance(restored[0].v, int)
    assert restored[1].v == "seven"
    assert isinstance(restored[1].v, str)


def test_json_marker_maps_optional_union_to_nullable_string_and_round_trips():
    class OptionalJsonUnionModel(BaseModel):
        payload: Annotated[dict[str, Any] | None, Json]

    schema = model_to_arrow_schema(OptionalJsonUnionModel)
    field = schema.field("payload")

    assert field.type == pa.string()
    assert field.nullable is True

    rows = [
        OptionalJsonUnionModel(payload=None),
        OptionalJsonUnionModel(payload={"name": "ava", "items": [1, {"ok": True}]}),
    ]

    assert (
        arrow_to_models(models_to_arrow(rows, OptionalJsonUnionModel), OptionalJsonUnionModel)
        == rows
    )


def test_unsupported_fields_raise_with_field_path():
    class BadAny(BaseModel):
        x: Any

    with pytest.raises(UnsupportedModelFieldError, match="x"):
        model_to_arrow_schema(BadAny)

    class BadUnion(BaseModel):
        y: int | str

    with pytest.raises(UnsupportedModelFieldError, match="y"):
        model_to_arrow_schema(BadUnion)

    class BadNested(BaseModel):
        settings: dict[str, Any]

    class BadOuter(BaseModel):
        profile: BadNested

    with pytest.raises(UnsupportedModelFieldError, match=r"profile\.settings"):
        model_to_arrow_schema(BadOuter)


def test_arrow_to_models_ignores_extra_columns():
    rows = [OptionalModel(required=1, maybe=None)]
    table = models_to_arrow(rows, OptionalModel)
    table = table.append_column(
        pa.field("_ava_run_id", pa.string()),
        pa.array(["run-1"], type=pa.string()),
    )

    assert arrow_to_models(table, OptionalModel) == rows


def test_arrow_to_models_accepts_polars_dataframe():
    rows = [NestedContainer(nested=NestedModel(label="x", value=2))]
    table = models_to_arrow(rows, NestedContainer)
    frame = pl.from_arrow(table)

    assert arrow_to_models(frame, NestedContainer) == rows


class FileRef:
    def __init__(self, path: str) -> None:
        self.path = path

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FileRef) and self.path == other.path

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: object,
        handler: object,
    ) -> core_schema.CoreSchema:
        def validate(value: object) -> FileRef:
            if isinstance(value, FileRef):
                return value
            if isinstance(value, str):
                return FileRef(value)
            raise ValueError("FileRef must be a string or FileRef")

        return core_schema.no_info_plain_validator_function(
            validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda ref: ref.path,
                return_schema=core_schema.str_schema(),
            ),
        )


def test_custom_scalar_serializing_type_maps_to_string_and_round_trips():
    class FileModel(BaseModel):
        ref: FileRef

    rows = [FileModel(ref=FileRef("/tmp/input.txt"))]
    schema = model_to_arrow_schema(FileModel)

    assert schema.field("ref").type == pa.string()
    assert arrow_to_models(models_to_arrow(rows, FileModel), FileModel) == rows


def test_json_is_importable_from_top_level():
    from avalanche import Json as TopLevelJson

    assert TopLevelJson is Json
