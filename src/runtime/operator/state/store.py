"""The operator's SQLite state store: one owned connection, locked and migrated."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ._transaction import sqlite_transaction
from .errors import StateStoreError, StateStoreLockedError
from .migrations import migrate

MEMORY = ":memory:"
DEFAULT_STATE_DB = Path(".avalanche") / "operator" / "state.db"

StateDbPath = str | os.PathLike[str]


class OperatorStateStore:
    """Own one SQLite connection to the operator state database.

    Opening a file takes an exclusive SQLite lock that is held until
    :meth:`close`, so a second operator or ``ava state`` command on the same
    file fails at once with :class:`StateStoreLockedError`. The operating
    system releases the lock when the owning process exits, even on
    ``kill -9``. ``:memory:`` opens a private, unlocked, throwaway database.

    Every statement runs through :meth:`transaction`, which serializes access
    from all threads with one lock. The operator calls it while holding its own
    ``RLock``; the store never calls back into the operator, so the lock order
    is always operator, then store.
    """

    def __init__(self, connection: sqlite3.Connection, path: Path | None) -> None:
        """Wrap an already opened, locked and migrated connection; use :meth:`open`."""
        self._connection = connection
        self._path = path
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def open(cls, path: StateDbPath) -> OperatorStateStore:
        """Open, lock and migrate the database at ``path`` (or ``:memory:``)."""
        if os.fspath(path) == MEMORY:
            connection = _connect(MEMORY)
            try:
                _configure(connection)
                migrate(connection, MEMORY)
            except BaseException:
                connection.close()
                raise
            return cls(connection, None)

        resolved = Path(path).expanduser().resolve()
        try:
            connection = _open_locked_file(resolved)
        except StateStoreError:
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            # A file that is not a database, a directory, or an unwritable folder.
            raise StateStoreError(
                f"Could not open operator state database {resolved}: {exc}"
            ) from exc
        return cls(connection, resolved)

    @property
    def path(self) -> Path | None:
        """The resolved database file, or ``None`` for ``:memory:``."""
        return self._path

    @property
    def location(self) -> str:
        """The database file path as text, or ``:memory:``."""
        return MEMORY if self._path is None else str(self._path)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run adapter reads and writes atomically, serialized across threads."""
        with self._lock:
            if self._closed:
                raise StateStoreError(f"Operator state database {self.location} is closed")
            if self._connection.in_transaction:
                raise StateStoreError("Operator state transactions cannot be nested")
            with sqlite_transaction(self._connection) as connection:
                yield connection

    def close(self) -> None:
        """Checkpoint, release the lock and close the connection; idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()


def _open_locked_file(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(str(path))
    try:
        try:
            # The lock is taken by the first statement that touches the file and, in
            # EXCLUSIVE locking mode, is never released until close. WAL in exclusive
            # mode keeps its index in heap memory, not in a -shm file.
            connection.execute("PRAGMA locking_mode = EXCLUSIVE")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise StateStoreLockedError(path) from exc
            raise
        if mode != "wal":
            raise StateStoreError(
                f"Operator state database {path} could not use WAL journaling "
                f"(SQLite chose {mode!r}). Put it on a local filesystem."
            )
        _configure(connection)
        migrate(connection, str(path))
    except BaseException:
        connection.close()
        raise
    return connection


def _connect(database: str) -> sqlite3.Connection:
    # timeout=0: a held lock fails immediately instead of waiting 5 seconds.
    # check_same_thread=False: the store's lock, not the thread, guards access.
    connection = sqlite3.connect(database, timeout=0, check_same_thread=False, autocommit=True)
    connection.row_factory = sqlite3.Row
    return connection


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    # NORMAL is crash-safe in WAL mode: a killed process loses nothing it committed;
    # only an OS crash or power loss can drop the last commits, never corrupt the file.
    connection.execute("PRAGMA synchronous = NORMAL")
