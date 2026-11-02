"""Explicit transactions on an autocommit ``sqlite3`` connection."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def sqlite_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block between BEGIN and COMMIT, rolling back if it raises."""
    connection.execute("BEGIN")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
