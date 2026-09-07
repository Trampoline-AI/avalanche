"""AppendResult conversion and cardinality boundaries."""

import polars as pl
import pytest
from pydantic import BaseModel

import avalanche as ava


class Row(BaseModel):
    id: int
    name: str


@pytest.mark.parametrize("format", ["polars", "arrow", "batch"])
def test_append_result_conversions_preserve_rows_and_typed_cardinality(format):
    frame = pl.DataFrame({"id": [2, 1], "name": ["second", "first"], "_ava_run_id": ["r", "r"]})
    data = frame if format == "polars" else frame.to_arrow()
    if format == "batch":
        data = data.to_batches()[0]
    result = ava.AppendResult(data=data, snapshot_id=1, row_model=Row)
    assert result.to_dicts() == frame.to_dicts()
    assert result.to_arrow().to_pylist() == frame.to_dicts()
    assert result.to_polars().to_dicts() == frame.to_dicts()
    assert result.to_models() == [Row(id=2, name="second"), Row(id=1, name="first")]
    with pytest.raises(ValueError):
        result.one()
    with pytest.raises(ValueError):
        result.one_or_none()

    single = ava.AppendResult(data=frame.head(1), snapshot_id=1, row_model=Row)
    assert single.one() == single.one_or_none() == Row(id=2, name="second")
    empty = ava.AppendResult(data=frame.clear(), snapshot_id=1, row_model=Row)
    assert empty.to_models() == []
    assert empty.one_or_none() is None
    with pytest.raises(ValueError):
        empty.one()


def test_untyped_append_result_cannot_materialize_models():
    result = ava.AppendResult(data=pl.DataFrame({"id": [1], "name": ["one"]}), snapshot_id=1)
    with pytest.raises(TypeError):
        result.to_models()
