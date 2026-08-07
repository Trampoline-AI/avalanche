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
    stream = ava.Stream(table, key="test_key", mode="append_scan")

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

    auto_pass_workflow().run(executor=ava.LocalExecutor()).result()


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
        def failing_step(
            data: pl.DataFrame = ava.Stream(MagicMock(), key="test", mode="append_scan"),
        ):
            assert data["x"].to_list() == [1, 2, 3]
            raise ValueError("Intentional failure")

        @ava.workflow
        def test_workflow():
            failing_step()

        with pytest.raises(ValueError, match="Intentional failure"):
            test_workflow().run(executor=ava.LocalExecutor()).result()

    assert exit_calls == ["exit_error:ValueError"]


def test_deferred_stream_metadata_mismatch_does_not_fetch_data(monkeypatch):
    """Table-identity mismatch must be decided on control metadata alone.

    Control/data-plane split invariant: when the deferred parent is an
    AppendResultHandle for a DIFFERENT table, resolution must return None
    (table-backed fallback) WITHOUT ever dereferencing the handle's data_ref.
    Fetching the frame to discover a mismatch would defeat the whole split.
    """
    import avalanche.runtime.providers.stream as stream_mod
    from avalanche.types import AppendResultHandle, DeferredStreamUpstream

    calls = []

    def _fail_get(ref):
        calls.append(ref)
        raise AssertionError("data_ref must not be fetched for a mismatched table")

    monkeypatch.setattr(stream_mod, "_ray_get", _fail_get)

    upstream = DeferredStreamUpstream(
        parent_kwarg="__ava_stream_parent_node_df",
        table_identity="target.table",
    )
    parent_value = AppendResultHandle(
        data_ref=object(),
        snapshot_id=123,
        table_identity="other.table",
    )

    result = stream_mod._resolve_deferred_stream_upstream(upstream, parent_value)
    assert result is None
    assert calls == [], calls


def test_deferred_stream_matching_metadata_fetches_data_once(monkeypatch):
    """A matching table identity resolves to AppendResult, fetching data once."""
    import avalanche.runtime.providers.stream as stream_mod
    from avalanche.types import AppendResult, AppendResultHandle, DeferredStreamUpstream

    frame = pl.DataFrame({"x": [1, 2, 3]})
    sentinel_ref = object()
    calls = []

    def _get(ref):
        calls.append(ref)
        assert ref is sentinel_ref
        return frame

    monkeypatch.setattr(stream_mod, "_ray_get", _get)

    upstream = DeferredStreamUpstream(
        parent_kwarg="__ava_stream_parent_node_df",
        table_identity="target.table",
    )
    parent_value = AppendResultHandle(
        data_ref=sentinel_ref,
        snapshot_id=7,
        table_identity="target.table",
    )

    result = stream_mod._resolve_deferred_stream_upstream(upstream, parent_value)
    assert isinstance(result, AppendResult)
    assert result.to_polars().equals(frame)
    assert calls == [sentinel_ref], calls
