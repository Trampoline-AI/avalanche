"""Backend-neutral CAS/progress contracts for storage tables."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import dataframely as dy
import polars as pl
import pytest

import avalanche as ava
from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable
from avalanche.lance import LanceNamespace, LanceNamespaceConfig, LanceTable


class CasSchema(dy.Schema):
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
            "catalog": "cas-contract-catalog",
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
def table(backend: BackendCase, tmp_path):
    class CasNamespace(backend.namespace_cls):
        ns_config = backend.namespace_config_cls(
            name=f"cas-{backend.name}",
            base_location=str(tmp_path),
        )
        records = backend.table_cls(schema=CasSchema)

    ns = CasNamespace(**backend.namespace_kwargs(str(tmp_path)))
    ns.push()
    return ns.records


def _rows(start: int, count: int = 1) -> pl.DataFrame:
    ids = list(range(start, start + count))
    return pl.DataFrame({"id": ids, "name": [f"name-{i}" for i in ids]})


def _append(table, doc_id: int):
    return table.append(_rows(doc_id))


def _data_snapshot_ids(table) -> list[int]:
    return [entry.snapshot_id for entry in table.history()]


def test_table_properties_transaction_supports_cursor(table):
    cursor = ava.Cursor(table, key="last_id")
    assert cursor.get() is None

    with cursor.transaction():
        cursor.set(123)

    table.refresh()
    assert cursor.get() == "123"


def test_progress_store_claims_marks_done_and_advances_cursor(table):
    r1 = table.append(_rows(1))
    r2 = table.append(_rows(2))
    assert _data_snapshot_ids(table) == [r1.snapshot_id, r2.snapshot_id]

    store = ava.ProgressStore(table, key="cas_progress", worker_id="worker-a")
    claimed = store.claim_next_pending()
    assert claimed == r1.snapshot_id
    assert claimed is not None

    competing = ava.ProgressStore(table, key="cas_progress", worker_id="worker-b")
    with pytest.raises(RuntimeError, match="cannot be claimed"):
        competing.claim(r1.snapshot_id)

    store.mark_done(claimed)
    assert store.advance_cursor() == r1.snapshot_id
    assert store.get_cursor() == r1.snapshot_id

    table.refresh()
    assert _data_snapshot_ids(table) == [r1.snapshot_id, r2.snapshot_id]
    assert store.claim_next_pending() == r2.snapshot_id


def test_progress_context_manager_marks_done(table):
    result = table.append(_rows(1))

    with ava.ProgressStore(table, key="ctx_progress", worker_id="worker-a") as snapshot_id:
        assert snapshot_id == result.snapshot_id

    store = ava.ProgressStore(table, key="ctx_progress", worker_id="worker-a")
    assert store.get_cursor() == result.snapshot_id
    assert store.list_pending() == []


def test_progress_context_manager_marks_failed(table):
    result = table.append(_rows(1))

    with pytest.raises(ValueError, match="Processing failed"):
        with ava.ProgressStore(table, key="ctx_failure", worker_id="worker-a") as snapshot_id:
            assert snapshot_id == result.snapshot_id
            raise ValueError("Processing failed")

    store = ava.ProgressStore(table, key="ctx_failure", worker_id="worker-a")
    metadata = store._get_snapshot_metadata(result.snapshot_id)
    assert metadata is not None
    assert metadata.state == ava.SnapshotState.FAILED
    assert "ValueError" in metadata.last_error
    assert store.get_cursor() is None


def test_cursor_advances_past_quarantined(table):
    r1 = _append(table, 1)
    r2 = _append(table, 2)
    r3 = _append(table, 3)

    store = ava.ProgressStore(table, key="quarantine_test", max_attempts=1)
    store.claim(r1.snapshot_id)
    store.mark_done(r1.snapshot_id)
    store.claim(r2.snapshot_id)
    store.mark_failed(r2.snapshot_id, error="permanent failure")
    store.claim(r3.snapshot_id)
    store.mark_done(r3.snapshot_id)

    assert store.advance_cursor() == r3.snapshot_id


def test_cursor_stops_at_pending(table):
    r1 = _append(table, 1)
    _append(table, 2)
    r3 = _append(table, 3)

    store = ava.ProgressStore(table, key="pending_stop_test")
    store.claim(r1.snapshot_id)
    store.mark_done(r1.snapshot_id)
    store.claim(r3.snapshot_id)
    store.mark_done(r3.snapshot_id)

    assert store.advance_cursor() == r1.snapshot_id


def test_cursor_stops_at_failed_not_quarantined(table):
    r1 = _append(table, 1)
    r2 = _append(table, 2)

    store = ava.ProgressStore(table, key="failed_stop_test", max_attempts=3)
    store.claim(r1.snapshot_id)
    store.mark_done(r1.snapshot_id)
    store.claim(r2.snapshot_id)
    store.mark_failed(r2.snapshot_id, error="temporary failure")

    metadata = store._get_snapshot_metadata(r2.snapshot_id)
    assert metadata is not None
    assert metadata.state == ava.SnapshotState.FAILED
    assert store.advance_cursor() == r1.snapshot_id


def test_prune_keeps_last_n_done_snapshots(table):
    results = [_append(table, i) for i in range(1, 6)]
    store = ava.ProgressStore(table, key="prune_test", max_done_history=2)

    for result in results:
        store.claim(result.snapshot_id)
        store.mark_done(result.snapshot_id)

    assert store.advance_cursor() == results[-1].snapshot_id

    for result in results[:3]:
        assert store._get_snapshot_metadata(result.snapshot_id) is None
    for result in results[3:]:
        metadata = store._get_snapshot_metadata(result.snapshot_id)
        assert metadata is not None
        assert metadata.state == ava.SnapshotState.DONE


def test_prune_does_not_remove_quarantined_snapshots(table):
    results = [_append(table, i) for i in range(1, 5)]
    store = ava.ProgressStore(
        table,
        key="prune_quarantine_test",
        max_done_history=1,
        max_attempts=1,
    )

    store.claim(results[0].snapshot_id)
    store.mark_failed(results[0].snapshot_id, error="permanent")
    for result in results[1:]:
        store.claim(result.snapshot_id)
        store.mark_done(result.snapshot_id)

    store.advance_cursor()

    metadata = store._get_snapshot_metadata(results[0].snapshot_id)
    assert metadata is not None
    assert metadata.state == ava.SnapshotState.QUARANTINED


def test_prune_disabled_with_zero(table):
    results = [_append(table, i) for i in range(1, 4)]
    store = ava.ProgressStore(table, key="no_prune_test", max_done_history=0)

    for result in results:
        store.claim(result.snapshot_id)
        store.mark_done(result.snapshot_id)

    store.advance_cursor()

    for result in results:
        assert store._get_snapshot_metadata(result.snapshot_id) is not None


def test_default_max_done_history(table):
    _append(table, 1)
    store = ava.ProgressStore(table, key="default_test")
    assert store.max_done_history == 10


def test_reset_clears_all_progress(table):
    results = [_append(table, i) for i in range(1, 4)]
    store = ava.ProgressStore(table, key="reset_test")

    for result in results:
        store.claim(result.snapshot_id)
        store.mark_done(result.snapshot_id)

    store.advance_cursor()
    assert store.get_cursor() == results[-1].snapshot_id

    store.reset()

    assert store.get_cursor() is None
    for result in results:
        assert store._get_snapshot_metadata(result.snapshot_id) is None


def test_expired_lease_appears_in_pending(table):
    result = _append(table, 1)
    store = ava.ProgressStore(table, key="expired_lease_test", lease_ttl_seconds=1)

    store.claim(result.snapshot_id)
    assert result.snapshot_id not in store.list_pending()

    time.sleep(2.1)

    assert result.snapshot_id in store.list_pending()


def test_can_reclaim_expired_lease(table):
    result = _append(table, 1)
    store1 = ava.ProgressStore(table, key="reclaim_test", lease_ttl_seconds=1)
    store2 = ava.ProgressStore(table, key="reclaim_test", lease_ttl_seconds=1)

    store1.claim(result.snapshot_id)
    with pytest.raises(RuntimeError, match="cannot be claimed"):
        store2.claim(result.snapshot_id)

    time.sleep(2.1)

    store2.claim(result.snapshot_id)
    metadata = store2._get_snapshot_metadata(result.snapshot_id)
    assert metadata is not None
    assert metadata.worker_id == store2.worker_id
    assert metadata.attempt == 2


def test_failed_snapshot_retries_until_quarantined(table):
    result = _append(table, 1)
    store = ava.ProgressStore(table, key="retry_test", max_attempts=3)

    store.claim(result.snapshot_id)
    store.mark_failed(result.snapshot_id, error="first failure")
    assert result.snapshot_id in store.list_pending()

    store.claim(result.snapshot_id)
    store.mark_failed(result.snapshot_id, error="second failure")
    assert result.snapshot_id in store.list_pending()

    store.claim(result.snapshot_id)
    store.mark_failed(result.snapshot_id, error="third failure")

    metadata = store._get_snapshot_metadata(result.snapshot_id)
    assert metadata is not None
    assert metadata.state == ava.SnapshotState.QUARANTINED
    assert result.snapshot_id not in store.list_pending()


def test_cursor_not_in_snapshot_history_starts_from_beginning(table):
    results = [_append(table, i) for i in range(1, 4)]
    store = ava.ProgressStore(table, key="cursor_history_test")

    with store.table.transaction() as tx:
        tx.set_properties(**{f"avalanche.stream.{store.key}.cursor": "99999999999"})

    pending = store.list_pending()
    assert len(pending) == 3
    assert results[0].snapshot_id in pending


def test_claim_next_pending_returns_none_when_candidate_already_claimed(table):
    result = _append(table, 1)
    store1 = ava.ProgressStore(table, key="cas_test")
    store2 = ava.ProgressStore(table, key="cas_test")

    snapshot = store1.claim_next_pending()
    assert snapshot == result.snapshot_id
    assert store2.claim_next_pending() is None

    metadata = store2._get_snapshot_metadata(snapshot)
    assert metadata is not None
    assert metadata.state == ava.SnapshotState.STARTED
    assert metadata.worker_id == store1.worker_id


def test_cursor_advancement_moves_forward_across_done_versions(table):
    result1 = _append(table, 1)
    result2 = _append(table, 2)
    store = ava.ProgressStore(table, key="cas_cursor_test")

    store.claim(result1.snapshot_id)
    store.mark_done(result1.snapshot_id)
    assert store.advance_cursor() == result1.snapshot_id

    store.claim(result2.snapshot_id)
    store.mark_done(result2.snapshot_id)
    assert store.advance_cursor() == result2.snapshot_id
    assert store.get_cursor() == result2.snapshot_id
