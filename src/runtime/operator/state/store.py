"""The operator's SQLite state store: one owned connection, locked and migrated."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ._transaction import sqlite_transaction
from .errors import StateStoreError, StateStoreLockedError
from .migrations import migrate

MEMORY = ":memory:"
DEFAULT_STATE_DB = Path(".avalanche") / "operator" / "state.db"

StateDbPath = str | os.PathLike[str]


@dataclass(frozen=True)
class PageUsage:
    """How many of the database's pages hold data and how many are free for reuse."""

    page_size: int
    page_count: int
    free_pages: int

    @property
    def free_bytes(self) -> int:
        return self.page_size * self.free_pages

    @property
    def free_fraction(self) -> float:
        return self.free_pages / self.page_count if self.page_count else 0.0


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

    def page_usage(self) -> PageUsage:
        """The database's page size, page count and free pages."""
        with self.transaction() as connection:
            return PageUsage(
                page_size=_pragma_int(connection, "page_size"),
                page_count=_pragma_int(connection, "page_count"),
                free_pages=_pragma_int(connection, "freelist_count"),
            )

    def compact(self, *, vacuum: bool) -> None:
        """Write the WAL back into the database file, rebuilding it first with ``vacuum``.

        ``VACUUM`` returns free pages to the file system; without it they stay in
        the file for SQLite to reuse. Neither can run inside a transaction.
        """
        with self._lock:
            if self._closed:
                raise StateStoreError(f"Operator state database {self.location} is closed")
            if self._connection.in_transaction:
                raise StateStoreError("Operator state compaction cannot run in a transaction")
            if vacuum:
                self._connection.execute("VACUUM")
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

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
        from .workspaces import restore_references

        restore_references(connection)
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


def _pragma_int(connection: sqlite3.Connection, name: str) -> int:
    value = connection.execute(f"PRAGMA {name}").fetchone()[0]
    if not isinstance(value, int):
        raise StateStoreError(f"SQLite returned {value!r} for PRAGMA {name}")
    return value


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    # An accepted receipt must survive an OS crash/power loss, not only SIGKILL.
    connection.execute("PRAGMA synchronous = FULL")
