"""Ordered schema migrations for the operator state database.

``MIGRATIONS`` is the complete, append-only history. Version ``n`` is at index
``n - 1``. Never edit or reorder a released migration: add a new one. Each
migration runs in its own transaction together with the ``schema_migrations``
row that records it, so a failed migration leaves the database at the previous
version. Tables use ``STRICT`` so SQLite rejects values of the wrong type.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from avalanche.run_keys import canonical_json

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
    Migration(
        version=3,
        name="run results",
        statements=(
            """
            CREATE TABLE run_results (
                run_id TEXT PRIMARY KEY,
                bundle_name TEXT NOT NULL UNIQUE,
                manifest_sha256 TEXT NOT NULL,
                byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                published_at REAL NOT NULL
            ) STRICT
            """,
        ),
    ),
    Migration(
        version=4,
        name="runs",
        statements=(
            # Structural run state; logs, events, traces and results live elsewhere.
            # process_group/process_started identify the run's coordinator process
            # group, so a restarted operator can stop it without hitting a reused id.
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                flow_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('requesting', 'pending', 'running', 'success', 'failed',
                               'cancelled')
                ),
                triggered_at REAL,
                started_at REAL,
                ended_at REAL,
                triggered_by TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                workflow_display_name TEXT NOT NULL,
                topology_json TEXT NOT NULL,
                input_json TEXT,
                terminal_seal_json TEXT,
                created_sequence INTEGER NOT NULL CHECK (created_sequence > 0),
                revision INTEGER NOT NULL CHECK (revision > 0),
                activity_sequence INTEGER NOT NULL CHECK (activity_sequence >= 0),
                request_json TEXT NOT NULL,
                process_group INTEGER,
                process_started TEXT,
                process_booted TEXT,
                CHECK ((process_group IS NULL) = (process_started IS NULL)),
                CHECK ((process_group IS NULL) = (process_booted IS NULL))
            ) STRICT
            """,
            "CREATE INDEX runs_by_created_sequence ON runs (created_sequence)",
            """
            CREATE TABLE run_nodes (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                node_id TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position >= 0),
                name TEXT NOT NULL,
                node_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'running', 'success', 'failed', 'skipped')
                ),
                started_at REAL,
                ended_at REAL,
                error TEXT,
                step_io_json TEXT,
                revision INTEGER NOT NULL CHECK (revision > 0),
                PRIMARY KEY (run_id, node_id),
                UNIQUE (run_id, position)
            ) STRICT
            """,
            # The staged files a run's input used, in attachment order.
            """
            CREATE TABLE run_input_files (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                position INTEGER NOT NULL CHECK (position >= 0),
                object_key TEXT NOT NULL,
                name TEXT NOT NULL,
                media_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                PRIMARY KEY (run_id, position)
            ) STRICT
            """,
            "CREATE INDEX run_input_files_by_object_key ON run_input_files (object_key)",
        ),
    ),
    Migration(
        version=5,
        name="run details",
        statements=(
            # A run's log, in append order. sequence is the run's log sequence, 1, 2, ...
            """
            CREATE TABLE run_logs (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                node_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR')),
                channel TEXT,
                attempt TEXT,
                message TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                PRIMARY KEY (run_id, sequence)
            ) STRICT
            """,
            "CREATE INDEX run_logs_by_node ON run_logs (run_id, node_id, sequence)",
            # Each agent step's live events, in the order the operator accepted them.
            """
            CREATE TABLE run_agent_events (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                node_id TEXT NOT NULL,
                event_sequence INTEGER NOT NULL CHECK (event_sequence > 0),
                invocation_id TEXT NOT NULL,
                event_json TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                native_event_kind TEXT NOT NULL,
                agent_event_kind TEXT NOT NULL,
                turn INTEGER,
                duration_ms INTEGER,
                error INTEGER NOT NULL CHECK (error IN (0, 1)),
                tool_call_count INTEGER NOT NULL CHECK (tool_call_count >= 0),
                PRIMARY KEY (run_id, node_id, event_sequence)
            ) STRICT
            """,
            # Each classifier step's invocation snapshots, in the order accepted.
            """
            CREATE TABLE run_classifier_events (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                node_id TEXT NOT NULL,
                event_sequence INTEGER NOT NULL CHECK (event_sequence > 0),
                invocation_id TEXT NOT NULL,
                invocation_index INTEGER NOT NULL,
                answers_json TEXT NOT NULL,
                event_json TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                event_kind TEXT NOT NULL,
                duration_ms INTEGER,
                error INTEGER NOT NULL CHECK (error IN (0, 1)),
                PRIMARY KEY (run_id, node_id, event_sequence)
            ) STRICT
            """,
            # The current trace descriptor of each agent step, and its last error.
            """
            CREATE TABLE run_traces (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                node_id TEXT NOT NULL,
                descriptor_json TEXT NOT NULL,
                error TEXT,
                PRIMARY KEY (run_id, node_id)
            ) STRICT
            """,
            # Finalized trace bodies, keyed by the revision that published each one.
            """
            CREATE TABLE run_trace_bodies (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                node_id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision > 0),
                body BLOB NOT NULL,
                PRIMARY KEY (run_id, node_id, revision)
            ) STRICT
            """,
            # Every published trace revision and the body it serves; a later revision
            # that did not finalize a new trace keeps serving the previous body.
            """
            CREATE TABLE run_trace_revisions (
                run_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision > 0),
                body_revision INTEGER NOT NULL CHECK (body_revision <= revision),
                PRIMARY KEY (run_id, node_id, revision),
                FOREIGN KEY (run_id, node_id, body_revision)
                    REFERENCES run_trace_bodies (run_id, node_id, revision) ON DELETE CASCADE
            ) STRICT
            """,
            # Evaluation records in submission order; record_json is the full record.
            """
            CREATE TABLE run_evaluations (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                evaluation_id TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position >= 0),
                node_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'failed')),
                record_json TEXT NOT NULL,
                PRIMARY KEY (run_id, evaluation_id),
                UNIQUE (run_id, position)
            ) STRICT
            """,
            "CREATE INDEX run_evaluations_by_status ON run_evaluations (status)",
        ),
    ),
    Migration(
        version=6,
        name="runtime map expansion",
        statements=(
            "ALTER TABLE runs ADD COLUMN expansions_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE run_nodes ADD COLUMN map_parent TEXT",
            "ALTER TABLE run_nodes ADD COLUMN map_index INTEGER CHECK (map_index >= 0)",
        ),
    ),
    Migration(
        version=7,
        name="durable wait decisions and receipts",
        statements=(
            """
            CREATE TABLE wait_decisions (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                position INTEGER NOT NULL CHECK (position >= 0),
                card_json TEXT NOT NULL,
                PRIMARY KEY (run_id, position)
            ) STRICT
            """,
            """
            CREATE TABLE wait_receipts (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                scope TEXT NOT NULL,
                address TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                PRIMARY KEY (run_id, scope, address, idempotency_key)
            ) STRICT
            """,
        ),
    ),
)

# Rebuild the parent table without renaming it first: child foreign keys must keep
# referring to `runs`. migrate() disables FK enforcement only for this atomic
# rebuild, then checks every relationship before committing.
MIGRATIONS += (
    Migration(
        version=8,
        name="machine runs and visit journal",
        statements=(
            MIGRATIONS[3]
            .statements[0]
            .replace("CREATE TABLE runs (", "CREATE TABLE runs_replacement (")
            .replace("'cancelled')", "'cancelled', 'waiting')")
            .replace(
                "process_booted TEXT,",
                "process_booted TEXT, expansions_json TEXT NOT NULL DEFAULT '{}', "
                "key TEXT, machine_json TEXT, read_state TEXT NOT NULL DEFAULT 'retained' "
                "CHECK (read_state IN ('retained', 'expired')),",
            ),
            "INSERT INTO runs_replacement SELECT *, NULL, NULL, 'retained' FROM runs",
            "DROP TABLE runs",
            "ALTER TABLE runs_replacement RENAME TO runs",
            "CREATE INDEX runs_by_created_sequence ON runs (created_sequence)",
            "CREATE UNIQUE INDEX runs_by_key ON runs (workflow_id, key) WHERE key IS NOT NULL",
            """
            CREATE TABLE machine_visits (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                visit INTEGER NOT NULL CHECK (visit > 0),
                state TEXT NOT NULL,
                catalog_revision TEXT NOT NULL,
                entered_at REAL NOT NULL,
                left_at REAL,
                record_json TEXT NOT NULL,
                PRIMARY KEY (run_id, visit)
            ) STRICT
            """,
            """
            CREATE TABLE machine_facts (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                tseq INTEGER NOT NULL CHECK (tseq > 0),
                visit INTEGER NOT NULL,
                at REAL NOT NULL,
                record_json TEXT NOT NULL,
                PRIMARY KEY (run_id, tseq)
            ) STRICT
            """,
            """
            CREATE TABLE wait_inbox (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                receipt_id TEXT NOT NULL,
                event TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                PRIMARY KEY (run_id, receipt_id)
            ) STRICT
            """,
        ),
    ),
)

MIGRATIONS += (Migration(version=9, name="canonical UTF-8 run keys", statements=()),)

MIGRATIONS += (
    Migration(
        version=10,
        name="typed step and visit results",
        statements=(
            """
            CREATE TABLE step_results (
                run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
                visit INTEGER NOT NULL CHECK (visit >= 0),
                node_id TEXT NOT NULL,
                value_json TEXT NOT NULL,
                PRIMARY KEY (run_id, visit, node_id)
            ) STRICT
            """,
        ),
    ),
    Migration(
        version=10,
        name="immutable workspace commits and run grants",
        statements=(
            "CREATE TABLE workspace_commits (reference TEXT PRIMARY KEY, "
            "path TEXT NOT NULL) STRICT",
            "CREATE TABLE workspace_grants (run_id TEXT NOT NULL, reference TEXT NOT NULL "
            "REFERENCES workspace_commits(reference), PRIMARY KEY(run_id, reference)) STRICT",
            "CREATE TABLE workspace_heads (run_id TEXT NOT NULL, mount TEXT NOT NULL, "
            "reference TEXT NOT NULL REFERENCES workspace_commits(reference), "
            "PRIMARY KEY(run_id, mount)) STRICT",
        ),
    ),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


def _canonicalize_run_keys(connection: sqlite3.Connection) -> None:
    # Both flow and machine keys used the old ASCII-escaped serializer. Rewrite
    # identity only: historical request/receipt bytes must remain unchanged.
    rows = connection.execute("SELECT run_id, key FROM runs WHERE key IS NOT NULL")
    while batch := rows.fetchmany(256):
        connection.executemany(
            "UPDATE runs SET key=? WHERE run_id=?",
            [(canonical_json(json.loads(row["key"])), row["run_id"]) for row in batch],
        )


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
        rebuild_runs = migration.version == 8
        if rebuild_runs:
            connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with sqlite_transaction(connection):
                for statement in migration.statements:
                    connection.execute(statement)
                if migration.version == 9:
                    _canonicalize_run_keys(connection)
                if rebuild_runs and connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise StateSchemaMismatchError("Run table rebuild broke a foreign key")
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (migration.version, migration.name, time.time()),
                )
        finally:
            if rebuild_runs:
                connection.execute("PRAGMA foreign_keys = ON")
    return supported
