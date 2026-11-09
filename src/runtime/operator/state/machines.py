"""Typed machine journal records; mutations belong to the machine owner thread."""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from avalanche.machine_spec import MachineSpec

from ..models import RunSnapshot


class JournalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstanceRecord(JournalModel):
    run_id: str
    machine: str
    key: list[JsonValue]
    state: str
    visit: int = Field(ge=1)
    status: Literal["active", "waiting", "interrupted", "needs_attention", "final", "closed"]
    spec: MachineSpec
    payload: JsonValue = None
    outcome: JsonValue = None
    internal_event: str | None = None
    internal_consumed: bool = False
    reason: str | None = None


class TransitionRecord(JournalModel):
    from_state: str = Field(alias="from")
    to: str | None
    event: str | None
    source: Literal["internal", "external", "timer", "cancel"]
    visit: int


class VisitRecord(JournalModel):
    visit: int
    state: str
    catalog_revision: str
    entered_at: float
    left_at: float | None = None
    disposition: Literal["current", "retained", "archived", "indeterminate"] = "current"
    transition: TransitionRecord | None = None
    snapshot: RunSnapshot | None = None


class TimerRecord(JournalModel):
    run_id: str
    visit: int
    timer_index: int
    due: float
    target: str
    fired_at: float | None = None
    reason: Literal["due", "catchup", "manual", "cancelled"] | None = None


class InboxRecord(JournalModel):
    receipt_id: str
    event: str
    payload: JsonValue
    size_bytes: int


class FactRecord(JournalModel):
    tseq: int
    at: float
    status: str
    event: str | None = None
    receipt_id: str | None = None
    source: Literal["external", "internal", "timer"] = "external"
    reason: str | None = None


def canonical_json(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class MachinesTable:
    @staticmethod
    def load(connection: sqlite3.Connection, run_id: str) -> InstanceRecord | None:
        row = connection.execute(
            "SELECT record_json FROM machine_instances WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else InstanceRecord.model_validate_json(row["record_json"])

    @staticmethod
    def all(connection: sqlite3.Connection) -> tuple[InstanceRecord, ...]:
        return tuple(
            InstanceRecord.model_validate_json(row["record_json"])
            for row in connection.execute("SELECT record_json FROM machine_instances")
        )

    @staticmethod
    def find(connection: sqlite3.Connection, machine: str, key: list[JsonValue]) -> str | None:
        row = connection.execute(
            "SELECT run_id FROM machine_instances WHERE machine = ? AND key_json = ?",
            (machine, canonical_json(key)),
        ).fetchone()
        return None if row is None else row["run_id"]

    @staticmethod
    def write(connection: sqlite3.Connection, record: InstanceRecord) -> None:
        connection.execute(
            "INSERT INTO machine_instances VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET state=excluded.state, visit=excluded.visit, "
            "status=excluded.status, record_json=excluded.record_json",
            (
                record.run_id,
                record.machine,
                canonical_json(record.key),
                record.state,
                record.visit,
                record.status,
                record.model_dump_json(),
            ),
        )

    @staticmethod
    def visit(connection: sqlite3.Connection, run_id: str, visit: int) -> VisitRecord:
        row = connection.execute(
            "SELECT record_json FROM machine_visits WHERE run_id = ? AND visit = ?",
            (run_id, visit),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown machine visit {visit}")
        return VisitRecord.model_validate_json(row["record_json"])

    @staticmethod
    def write_visit(connection: sqlite3.Connection, run_id: str, record: VisitRecord) -> None:
        connection.execute(
            "INSERT INTO machine_visits VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, visit) DO UPDATE SET left_at=excluded.left_at, "
            "record_json=excluded.record_json",
            (
                run_id,
                record.visit,
                record.state,
                record.catalog_revision,
                record.entered_at,
                record.left_at,
                record.model_dump_json(by_alias=True),
            ),
        )

    @staticmethod
    def timers(
        connection: sqlite3.Connection, run_id: str, visit: int
    ) -> tuple[TimerRecord, ...]:
        return tuple(
            TimerRecord.model_validate(dict(row))
            for row in connection.execute(
                "SELECT * FROM machine_timers WHERE run_id = ? AND visit = ? "
                "AND reason IS NULL ORDER BY due, timer_index",
                (run_id, visit),
            )
        )

    @staticmethod
    def write_timer(connection: sqlite3.Connection, record: TimerRecord) -> None:
        connection.execute(
            "INSERT INTO machine_timers VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, visit, timer_index) DO UPDATE SET "
            "fired_at=excluded.fired_at, reason=excluded.reason",
            (
                record.run_id,
                record.visit,
                record.timer_index,
                record.due,
                record.target,
                record.fired_at,
                record.reason,
            ),
        )

    @staticmethod
    def inbox(connection: sqlite3.Connection, run_id: str) -> tuple[InboxRecord, ...]:
        return tuple(
            InboxRecord(
                receipt_id=row["receipt_id"],
                event=row["event"],
                payload=json.loads(row["payload_json"]),
                size_bytes=row["size_bytes"],
            )
            for row in connection.execute(
                "SELECT * FROM wait_inbox WHERE run_id = ? ORDER BY rowid", (run_id,)
            )
        )

    @staticmethod
    def fact(
        connection: sqlite3.Connection,
        run_id: str,
        visit: int,
        *,
        status: str,
        event: str | None = None,
        receipt_id: str | None = None,
        source: Literal["external", "internal", "timer"] = "external",
        reason: str | None = None,
        at: float,
    ) -> None:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(tseq), 0) + 1 FROM machine_facts WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        record = FactRecord(
            tseq=sequence,
            at=at,
            status=status,
            event=event,
            receipt_id=receipt_id,
            source=source,
            reason=reason,
        )
        connection.execute(
            "INSERT INTO machine_facts VALUES (?, ?, ?, ?, ?)",
            (run_id, sequence, visit, at, record.model_dump_json()),
        )
