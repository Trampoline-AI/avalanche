"""Backend-free unit tests for Stream provider mechanics."""

from unittest.mock import MagicMock, patch

import polars as pl
import pyarrow as pa
import pytest

import avalanche as ava


def test_append_result_converts_between_polars_and_arrow():
    df = pl.DataFrame({"id": [1, 2], "value": ["a", "b"]})
    result = ava.AppendResult(data=df, snapshot_id=123)

    assert result.snapshot_id == 123
    assert result.to_polars().equals(df)
    assert result.to_arrow().to_pydict() == df.to_arrow().to_pydict()
    assert result.to_dicts() == [{"id": 1, "value": "a"}, {"id": 2, "value": "b"}]


def test_append_result_converts_record_batch_to_dicts():
    batch = pa.record_batch(
        [pa.array([1, 2]), pa.array(["a", "b"])],
        names=["id", "value"],
    )
    result = ava.AppendResult(data=batch, snapshot_id=123)

    assert result.to_arrow().to_pydict() == {"id": [1, 2], "value": ["a", "b"]}
    assert result.to_polars().to_dicts() == result.to_dicts()
    assert result.to_dicts() == [{"id": 1, "value": "a"}, {"id": 2, "value": "b"}]


def test_stream_provider_marker():
    table = MagicMock()
    stream = ava.Stream(table, key="test_key")

    assert isinstance(stream, ava.Stream)
    assert stream.table is table
    assert stream.key == "test_key"
    assert ava.Stream.can_resolve(stream) is True


def test_auto_passing_returns_without_storage_backend():
    @ava.source
    def create_data():
        return pl.DataFrame({"x": [1, 2, 3]})

    @ava.step
    def double_values(df: pl.DataFrame):
        return df.with_columns(pl.col("x") * 2)

    @ava.dest
    def verify_data(df: pl.DataFrame):
        assert list(df["x"]) == [2, 4, 6]

    @ava.workflow
    def auto_pass_workflow():
        create_data() >> double_values() >> verify_data()

    auto_pass_workflow().run(executor=ava.LocalExecutor())


def test_stream_wrapper_does_not_double_exit():
    """Stream wrapper should not call __exit__ twice on error."""

    exit_calls = []

    class MockCM:
        def __enter__(self):
            return pl.DataFrame({"x": [1, 2, 3]})

        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type:
                exit_calls.append(f"exit_error:{exc_type.__name__}")
            else:
                exit_calls.append("exit_success")
            return False

    with patch("avalanche.runtime.providers.stream.consume_stream", return_value=MockCM()):

        @ava.step
        def failing_step(data: pl.DataFrame = ava.Stream(MagicMock(), key="test")):
            assert data["x"].to_list() == [1, 2, 3]
            raise ValueError("Intentional failure")

        @ava.workflow
        def test_workflow():
            failing_step()

        with pytest.raises(ValueError, match="Intentional failure"):
            test_workflow().run(executor=ava.LocalExecutor())

    assert exit_calls == ["exit_error:ValueError"]
