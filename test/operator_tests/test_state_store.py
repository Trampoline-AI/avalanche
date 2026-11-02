"""Operator SQLite state store: migrations, version refusal, locking and adapters."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager

import pytest
from pydantic import BaseModel, ConfigDict

from runtime.operator.operator import Operator
from runtime.operator.state import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    Migration,
    OperatorStateStore,
    StateRowError,
    StateSchemaMismatchError,
    StateSchemaTooNewError,
    StateStoreError,
    StateStoreLockedError,
)
from runtime.operator.state.metadata import OperatorMetadataTable
from runtime.operator.state.migrations import applied_migrations, migrate
from runtime.operator.state.rows import json_column, row_model

_HOLD_STORE = """
import sys
from runtime.operator.state import OperatorStateStore
store = OperatorStateStore.open(sys.argv[1])
print("locked", flush=True)
sys.stdin.read()
"""


@contextmanager
def _raw(path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, autocommit=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _versions(path) -> list[int]:
    with _raw(path) as connection:
        return [
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations")
        ]


def test_new_file_is_created_migrated_and_in_wal_mode(tmp_path):
    path = tmp_path / "nested" / "state.db"
    store = OperatorStateStore.open(path)
    try:
        assert store.path == path.resolve()
        with store.transaction() as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert [m.version for m in applied_migrations(connection)] == list(
                range(1, LATEST_SCHEMA_VERSION + 1)
            )
            metadata = OperatorMetadataTable.read(connection)
        assert (metadata.update_sequence, metadata.catalog_sequence) == (0, 0)
        assert metadata.created_at > 0
    finally:
        store.close()


def test_relative_path_resolves_against_the_start_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = OperatorStateStore.open(".avalanche/operator/state.db")
    store.close()
    assert store.path == (tmp_path / ".avalanche" / "operator" / "state.db").resolve()
    assert store.path.is_file()


def test_reopening_keeps_data_and_applies_nothing_twice(tmp_path):
    path = tmp_path / "state.db"
    store = OperatorStateStore.open(path)
    with store.transaction() as connection:
        OperatorMetadataTable.write_sequences(connection, update_sequence=7, catalog_sequence=3)
    store.close()

    store = OperatorStateStore.open(path)
    try:
        with store.transaction() as connection:
            metadata = OperatorMetadataTable.read(connection)
        assert (metadata.update_sequence, metadata.catalog_sequence) == (7, 3)
    finally:
        store.close()
    assert _versions(path) == list(range(1, LATEST_SCHEMA_VERSION + 1))


def test_older_database_is_migrated_in_order(tmp_path):
    path = tmp_path / "state.db"
    second = Migration(
        version=LATEST_SCHEMA_VERSION + 1,
        name="example table",
        statements=(
            "CREATE TABLE example (id INTEGER PRIMARY KEY, value TEXT NOT NULL) STRICT",
            "INSERT INTO example (id, value) SELECT 1, 'kept' FROM operator_metadata",
        ),
    )
    with _raw(path) as connection:
        assert migrate(connection, str(path)) == LATEST_SCHEMA_VERSION
        OperatorMetadataTable.write_sequences(connection, update_sequence=5, catalog_sequence=1)
        assert migrate(connection, str(path), (*MIGRATIONS, second)) == second.version
        assert connection.execute("SELECT value FROM example").fetchone()[0] == "kept"
        assert OperatorMetadataTable.read(connection).update_sequence == 5
    assert _versions(path) == [*range(1, LATEST_SCHEMA_VERSION + 1), second.version]


def test_failed_migration_leaves_the_previous_version(tmp_path):
    path = tmp_path / "state.db"
    broken = Migration(
        version=LATEST_SCHEMA_VERSION + 1,
        name="broken",
        statements=(
            "CREATE TABLE half_done (id INTEGER PRIMARY KEY) STRICT",
            "INSERT INTO missing_table VALUES (1)",
        ),
    )
    with _raw(path) as connection:
        migrate(connection, str(path))
        with pytest.raises(sqlite3.OperationalError):
            migrate(connection, str(path), (*MIGRATIONS, broken))
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "half_done" not in tables
    assert _versions(path) == list(range(1, LATEST_SCHEMA_VERSION + 1))


def test_newer_schema_version_is_refused_without_changes(tmp_path):
    path = tmp_path / "state.db"
    OperatorStateStore.open(path).close()
    future = LATEST_SCHEMA_VERSION + 1
    with _raw(path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, 'future', 0)",
            (future,),
        )

    with pytest.raises(StateSchemaTooNewError) as caught:
        OperatorStateStore.open(path)

    assert (caught.value.found, caught.value.supported) == (future, LATEST_SCHEMA_VERSION)
    assert str(path.resolve()) in str(caught.value)
    assert _versions(path) == [*range(1, LATEST_SCHEMA_VERSION + 1), future]
    # The refusal released the lock, so another opener can lock the file.
    with _raw(path) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute("COMMIT")


@pytest.mark.parametrize(
    "recorded",
    (
        pytest.param([(1, "something else")], id="renamed"),
        pytest.param([(2, "operator metadata")], id="gap"),
    ),
)
def test_divergent_migration_history_is_refused(tmp_path, recorded):
    path = tmp_path / "state.db"
    with _raw(path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL) STRICT"
        )
        connection.executemany(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, 0)",
            recorded,
        )
        with pytest.raises(StateSchemaMismatchError):
            migrate(connection, str(path), (MIGRATIONS[0], Migration(2, "two", ())))


def test_migration_versions_must_be_contiguous(tmp_path):
    with _raw(tmp_path / "state.db") as connection, pytest.raises(ValueError):
        migrate(connection, "x", (Migration(2, "two", ()),))


def test_second_store_on_the_same_file_is_refused_until_the_first_closes(tmp_path):
    path = tmp_path / "state.db"
    first = OperatorStateStore.open(path)
    try:
        with pytest.raises(StateStoreLockedError) as caught:
            OperatorStateStore.open(path)
        assert caught.value.path == path.resolve()
        assert str(path.resolve()) in str(caught.value)
        with first.transaction() as connection:
            assert OperatorMetadataTable.read(connection).update_sequence == 0
    finally:
        first.close()
    OperatorStateStore.open(path).close()


def test_lock_is_held_across_processes_and_released_when_the_holder_is_killed(tmp_path):
    path = tmp_path / "state.db"
    with subprocess.Popen(
        [sys.executable, "-c", _HOLD_STORE, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as holder:
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "locked"
            with pytest.raises(StateStoreLockedError):
                OperatorStateStore.open(path)
        finally:
            # SIGKILL on POSIX, TerminateProcess on Windows: no cleanup runs.
            holder.kill()
            holder.wait(timeout=10)
    OperatorStateStore.open(path).close()


def test_memory_stores_are_private_and_unlocked():
    first = OperatorStateStore.open(":memory:")
    second = OperatorStateStore.open(":memory:")
    try:
        assert first.path is None and first.location == ":memory:"
        with first.transaction() as connection:
            OperatorMetadataTable.write_sequences(
                connection, update_sequence=9, catalog_sequence=0
            )
        with second.transaction() as connection:
            assert OperatorMetadataTable.read(connection).update_sequence == 0
    finally:
        first.close()
        second.close()


def test_transaction_rolls_back_rejects_nesting_and_closed_use():
    store = OperatorStateStore.open(":memory:")
    with pytest.raises(RuntimeError):
        with store.transaction() as connection:
            OperatorMetadataTable.write_sequences(
                connection, update_sequence=4, catalog_sequence=4
            )
            raise RuntimeError("abort")
    with store.transaction() as connection:
        assert OperatorMetadataTable.read(connection).update_sequence == 0
        with pytest.raises(StateStoreError):
            with store.transaction():
                pass
    store.close()
    store.close()
    with pytest.raises(StateStoreError):
        with store.transaction():
            pass


def test_transactions_from_many_threads_are_serialized(tmp_path):
    store = OperatorStateStore.open(tmp_path / "state.db")
    errors: list[BaseException] = []

    def increment() -> None:
        try:
            for _ in range(50):
                with store.transaction() as connection:
                    current = OperatorMetadataTable.read(connection)
                    OperatorMetadataTable.write_sequences(
                        connection,
                        update_sequence=current.update_sequence + 1,
                        catalog_sequence=current.catalog_sequence,
                    )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=increment) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert errors == []
        with store.transaction() as connection:
            assert OperatorMetadataTable.read(connection).update_sequence == 400
    finally:
        store.close()


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    count: int


def test_row_and_json_adapters_raise_on_invalid_values():
    with closing(sqlite3.connect(":memory:")) as connection:
        _check_row_adapters(connection)


def _check_row_adapters(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    row = connection.execute("SELECT 'not a number' AS count").fetchone()
    with pytest.raises(StateRowError, match="example"):
        row_model(_Payload, row, table="example")
    assert row_model(
        _Payload, connection.execute("SELECT 3 AS count").fetchone(), table="t"
    ) == (_Payload(count=3))
    with pytest.raises(StateRowError, match="example.payload"):
        json_column(_Payload, '{"count": "3"}', table="example", column="payload")
    assert json_column(_Payload, '{"count": 3}', table="t", column="c") == _Payload(count=3)


def test_corrupt_metadata_row_is_an_error_not_skipped(tmp_path):
    path = tmp_path / "state.db"
    OperatorStateStore.open(path).close()
    with _raw(path) as connection:
        connection.execute("DELETE FROM operator_metadata")
    store = OperatorStateStore.open(path)
    try:
        with pytest.raises(StateRowError), store.transaction() as connection:
            OperatorMetadataTable.read(connection)
    finally:
        store.close()


def test_second_operator_on_the_same_database_fails_and_the_first_keeps_running(tmp_path):
    path = tmp_path / "state.db"
    first = Operator([], watch=False, schedule=False, webhook_port=0, state_db=path)
    try:
        with pytest.raises(StateStoreLockedError):
            Operator([], watch=False, schedule=False, webhook_port=0, state_db=path)
        assert first.get_catalog().workflows == ()
    finally:
        first.close()
    Operator([], watch=False, schedule=False, webhook_port=0, state_db=path).close()


def test_operator_releases_the_database_when_discovery_fails(tmp_path):
    path = tmp_path / "state.db"
    broken = tmp_path / "broken.py"
    broken.write_text("import missing_module_for_state_store_test\n")
    with pytest.raises(Exception, match="missing_module_for_state_store_test"):
        Operator([str(broken)], watch=False, schedule=False, webhook_port=0, state_db=path)
    OperatorStateStore.open(path).close()


def test_error_after_sqlite_rolled_back_by_itself_keeps_the_original_error():
    store = OperatorStateStore.open(":memory:")
    try:
        with store.transaction() as connection:
            connection.execute(
                "CREATE TABLE pairs (id INTEGER PRIMARY KEY ON CONFLICT ROLLBACK)"
            )
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as connection:
            connection.execute("INSERT INTO pairs (id) VALUES (1)")
            connection.execute("INSERT INTO pairs (id) VALUES (1)")
        with store.transaction() as connection:
            assert connection.execute("SELECT count(*) FROM pairs").fetchone()[0] == 0
    finally:
        store.close()


def test_failed_commit_rolls_back_and_the_store_stays_usable():
    store = OperatorStateStore.open(":memory:")
    try:
        with store.transaction() as connection:
            connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            connection.execute(
                "CREATE TABLE child (parent_id INTEGER REFERENCES parent (id) "
                "DEFERRABLE INITIALLY DEFERRED)"
            )
        # A deferred foreign key is checked at COMMIT, so the commit itself fails.
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as connection:
            connection.execute("INSERT INTO child (parent_id) VALUES (42)")
        with store.transaction() as connection:
            assert connection.execute("SELECT count(*) FROM child").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize("problem", ["not a database", "directory", "parent is a file"])
def test_unusable_paths_fail_with_a_state_store_error(tmp_path, problem):
    path = tmp_path / "state.db"
    if problem == "not a database":
        path.write_bytes(b"this is not an SQLite database, just some text" * 4)
    elif problem == "directory":
        path.mkdir()
    else:
        (tmp_path / "blocker").write_text("")
        path = tmp_path / "blocker" / "state.db"
    with pytest.raises(StateStoreError) as caught:
        OperatorStateStore.open(path)
    assert str(path.resolve()) in str(caught.value)


def test_operator_releases_the_database_when_later_startup_fails(tmp_path, monkeypatch):
    from runtime.operator import operator as operator_module

    def failing_result_store(*_args, **_kwargs):
        raise OSError("result folder unavailable")

    monkeypatch.setattr(operator_module, "ResultStore", failing_result_store)
    path = tmp_path / "state.db"
    with pytest.raises(OSError, match="result folder unavailable"):
        Operator([], watch=False, schedule=False, webhook_port=0, state_db=path)
    OperatorStateStore.open(path).close()
