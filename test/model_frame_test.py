"""Tests for pydantic model frame conversion."""

from __future__ import annotations

from datetime import date, datetime
from enum import IntEnum, StrEnum
from typing import Annotated, Any

import polars as pl
import pyarrow as pa
import pytest
from pydantic import BaseModel
from pydantic_core import core_schema

from avalanche import Json
from avalanche.model_frame import (
    UnsupportedModelFieldError,
    arrow_to_models,
    model_to_arrow_schema,
    models_to_arrow,
)


class Status(StrEnum):
    READY = "ready"
    DONE = "done"


class Rank(IntEnum):
    LOW = 1
    HIGH = 2


def test_models_round_trip_with_nested_lists_optional_enums_and_datetimes():
    class Comment(BaseModel):
        body: str
        created_at: datetime
        content: bytes
        context: Annotated[dict[str, str], Json]

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
        starts_on: date
        content: bytes
        payload: Annotated[dict[str, Any] | None, Json]
        variant: Annotated[int | str, Json]
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
            starts_on=date(2026, 1, 2),
            content=b"\x00binary\xff",
            payload={"items": [1, {"ok": True}]},
            variant=7,
            comments=[
                Comment(
                    body="ok",
                    created_at=datetime(2026, 1, 3, 4, 5, 6),
                    content=b"\xffnested",
                    context={"source": "review"},
                )
            ],
        ),
        Record(
            name="second",
            when=datetime(2026, 2, 3, 4, 5, 6),
            maybe=9,
            status=Status.DONE,
            rank=Rank.HIGH,
            profile=Profile(handle="beta", visits=5),
            tags=[],
            starts_on=date(2026, 2, 3),
            content=b"",
            payload=None,
            variant="seven",
            comments=[],
        ),
    ]

    table = models_to_arrow(rows, Record)
    table = table.append_column("_ava_run_id", pa.array(["run-1", "run-1"]))
    assert arrow_to_models(table, Record) == rows
    assert arrow_to_models(pl.from_arrow(table), Record) == rows


def test_unsupported_fields_raise_with_field_path():
    class BadNested(BaseModel):
        settings: dict[str, Any]

    class BadOuter(BaseModel):
        profile: BadNested

    with pytest.raises(UnsupportedModelFieldError, match=r"profile\.settings"):
        model_to_arrow_schema(BadOuter)


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
