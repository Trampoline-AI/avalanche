"""Tests for AppendResult conversion and typed row access."""

import polars as pl
import pyarrow as pa
import pytest
from pydantic import BaseModel

import avalanche as ava


class Row(BaseModel):
    id: int
    name: str


def _arrow_table(rows: list[tuple[int, str]]) -> pa.Table:
    return pa.table(
        {
            "id": pa.array([row_id for row_id, _ in rows], type=pa.int64()),
            "name": pa.array([name for _, name in rows], type=pa.string()),
        }
    )


def _typed_result(rows: list[tuple[int, str]]) -> ava.AppendResult[Row]:
    return ava.AppendResult(data=_arrow_table(rows), snapshot_id=1, row_model=Row)


def test_to_models_from_arrow_table_preserves_order():
    result = _typed_result([(2, "second"), (1, "first")])

    models = result.to_models()

    assert all(isinstance(model, Row) for model in models)
    assert [(model.id, model.name) for model in models] == [(2, "second"), (1, "first")]


def test_to_models_from_polars_dataframe_ignores_ava_columns():
    df = pl.DataFrame(
        {
            "id": [3, 4],
            "name": ["third", "fourth"],
            "_ava_run_id": ["run-a", "run-b"],
        }
    )
    result = ava.AppendResult(data=df, snapshot_id=2, row_model=Row)

    models = result.to_models()

    assert all(isinstance(model, Row) for model in models)
    assert [(model.id, model.name) for model in models] == [(3, "third"), (4, "fourth")]


def test_one_returns_single_model():
    model = _typed_result([(1, "only")]).one()

    assert isinstance(model, Row)
    assert model == Row(id=1, name="only")


def test_one_raises_for_zero_rows():
    with pytest.raises(ValueError, match="Expected exactly one row for Row; got 0 rows"):
        _typed_result([]).one()


def test_one_raises_for_two_rows():
    with pytest.raises(ValueError, match="Expected exactly one row for Row; got 2 rows"):
        _typed_result([(1, "first"), (2, "second")]).one()


def test_one_or_none_returns_none_for_zero_rows():
    assert _typed_result([]).one_or_none() is None


def test_one_or_none_returns_single_model():
    model = _typed_result([(1, "only")]).one_or_none()

    assert isinstance(model, Row)
    assert model == Row(id=1, name="only")


def test_one_or_none_raises_for_two_rows():
    with pytest.raises(ValueError, match="Expected exactly one row for Row; got 2 rows"):
        _typed_result([(1, "first"), (2, "second")]).one_or_none()


def test_typed_methods_raise_type_error_without_row_model():
    result = ava.AppendResult(data=_arrow_table([(1, "first")]), snapshot_id=1)

    for method_name in ("to_models", "one", "one_or_none"):
        with pytest.raises(TypeError, match="did not come from a table declared"):
            getattr(result, method_name)()


def test_backward_compatible_construction_and_conversion_helpers():
    df = pl.DataFrame({"id": [1, 2], "name": ["first", "second"]})

    keyword_result = ava.AppendResult(data=df, snapshot_id=3)
    positional_result = ava.AppendResult(df, 7)

    assert keyword_result.snapshot_id == 3
    assert positional_result.snapshot_id == 7
    assert keyword_result.row_model is None
    assert positional_result.row_model is None
    assert keyword_result.to_polars().equals(df)
    assert positional_result.to_arrow().to_pydict() == df.to_arrow().to_pydict()
    assert positional_result.to_dicts() == [
        {"id": 1, "name": "first"},
        {"id": 2, "name": "second"},
    ]
