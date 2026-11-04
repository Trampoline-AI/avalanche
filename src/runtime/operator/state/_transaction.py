"""Explicit transactions on an autocommit ``sqlite3`` connection."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def sqlite_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block between BEGIN and COMMIT, rolling back if it or the commit fails.

    SQLite already rolls back by itself after some errors (a full disk, I/O
    errors, ``ON CONFLICT ROLLBACK``), so the rollback runs only while a
    transaction is still open. The original error is always the one raised.
    """
    connection.execute("BEGIN")
    try:
        yield connection
        connection.execute("COMMIT")
    except BaseException as failure:
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error as exc:
                failure.add_note(f"Operator state rollback also failed: {exc}")
        raise
