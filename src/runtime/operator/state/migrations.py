"""Ordered schema migrations for the operator state database.

``MIGRATIONS`` is the complete, append-only history. Version ``n`` is at index
``n - 1``. Never edit or reorder a released migration: add a new one. Each
migration runs in its own transaction together with the ``schema_migrations``
row that records it, so a failed migration leaves the database at the previous
version. Tables use ``STRICT`` so SQLite rejects values of the wrong type.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from ._transaction import sqlite_transaction
from .errors import StateSchemaMismatchError, StateSchemaTooNewError
from .rows import row_model


@dataclass(frozen=True)
class Migration:
    """One schema step: DDL and data statements applied in a single transaction."""

    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="operator metadata",
        statements=(
            """
            CREATE TABLE operator_metadata (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                created_at REAL NOT NULL,
                update_sequence INTEGER NOT NULL CHECK (update_sequence >= 0),
                catalog_sequence INTEGER NOT NULL CHECK (catalog_sequence >= 0)
            ) STRICT
            """,
            """
            INSERT INTO operator_metadata (id, created_at, update_sequence, catalog_sequence)
            VALUES (1, (julianday('now') - 2440587.5) * 86400.0, 0, 0)
            """,
        ),
    ),
    Migration(
        version=2,
        name="schedules",
        statements=(
            # One row per workflow whose schedule has state worth keeping. enabled is
            # NULL until someone turns the schedule on or off; input_json is NULL while
            # runs use the workflow's defaults; enabled_minute is the minute it was
            # last turned on, whose own occurrence it never fires.
            """
            CREATE TABLE schedules (
                workflow_id TEXT PRIMARY KEY,
                enabled INTEGER CHECK (enabled IN (0, 1)),
                enabled_minute REAL,
                input_json TEXT,
                input_files_json TEXT NOT NULL,
                last_triggered REAL
            ) STRICT
            """,
        ),
    ),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version

_CREATE_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at REAL NOT NULL
) STRICT
"""


class AppliedMigration(BaseModel):
    """One ``schema_migrations`` row: a migration recorded as applied."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    version: int
    name: str
    applied_at: float


def applied_migrations(connection: sqlite3.Connection) -> tuple[AppliedMigration, ...]:
    """Read the recorded migration history in version order."""
    rows = connection.execute(
        "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    return tuple(row_model(AppliedMigration, row, table="schema_migrations") for row in rows)


def migrate(
    connection: sqlite3.Connection,
    location: str,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> int:
    """Apply pending migrations in order and return the resulting schema version.

    ``connection`` must use ``sqlite3.Row`` rows and autocommit. A database
    recorded at a newer version than ``migrations`` knows is refused before
    anything is written. A recorded history that differs from ``migrations``
    (a gap, or another name for the same version) is refused too, rather than
    guessed at.
    """
    if [migration.version for migration in migrations] != list(range(1, len(migrations) + 1)):
        raise ValueError("Migration versions must be 1, 2, 3, ... in order")
    with sqlite_transaction(connection):
        connection.execute(_CREATE_SCHEMA_MIGRATIONS)
        applied = applied_migrations(connection)
    supported = len(migrations)
    if applied and applied[-1].version > supported:
        raise StateSchemaTooNewError(location, applied[-1].version, supported)
    for recorded, expected in zip(applied, migrations, strict=False):
        if (recorded.version, recorded.name) != (expected.version, expected.name):
            raise StateSchemaMismatchError(
                f"Operator state database {location} records migration "
                f"{recorded.version} {recorded.name!r}, but this Avalanche version "
                f"expects migration {expected.version} {expected.name!r}. Pass a "
                "different --state-db."
            )
    for migration in migrations[len(applied) :]:
        with sqlite_transaction(connection):
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, time.time()),
            )
    return supported
