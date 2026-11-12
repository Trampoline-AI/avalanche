"""Typed machine journal records; mutations belong to the machine owner thread."""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from avalanche.machine_spec import MachineSpec
from avalanche.run_keys import canonical_json as canonical_key_json

from ..models import RunSnapshot, RunStatus


class JournalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstanceRecord(JournalModel):
    run_id: str = Field(exclude=True)
    machine: str = Field(exclude=True)
    key: list[JsonValue] = Field(exclude=True)
    run_status: RunStatus = Field(default=RunStatus.PENDING, exclude=True)
    state: str
    visit: int = Field(ge=1)
    spec: MachineSpec
    payload: JsonValue = None
    outcome: JsonValue = None
    internal_event: str | None = None
    internal_consumed: bool = False
    reason: str | None = None

    @property
    def phase(self) -> str:
        """Derived execution detail, never a second persisted lifecycle status."""
        if self.run_status == RunStatus.SUCCESS:
            return "final"
        if self.run_status in {RunStatus.CANCELLED, RunStatus.FAILED}:
            return "closed"
        if self.run_status != RunStatus.WAITING:
            return "active"
        if self.reason == "outcome_unknown":
            return "interrupted"
        if self.reason == "state_missing":
            return "needs_attention"
        return "waiting"


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


def journal_json(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class MachinesTable:
    @staticmethod
    def load(connection: sqlite3.Connection, run_id: str) -> InstanceRecord | None:
        row = connection.execute(
            "SELECT run_id, workflow_id, key, status, machine_json FROM runs "
            "WHERE run_id=? AND machine_json IS NOT NULL",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return InstanceRecord.model_validate(
            {
                **json.loads(row["machine_json"]),
                "run_id": row["run_id"],
                "machine": row["workflow_id"],
                "key": json.loads(row["key"]),
                "run_status": RunStatus(row["status"]),
            }
        )

    @staticmethod
    def all(connection: sqlite3.Connection) -> tuple[InstanceRecord, ...]:
        records = []
        for row in connection.execute(
            "SELECT run_id FROM runs WHERE machine_json IS NOT NULL AND read_state='retained'"
        ):
            record = MachinesTable.load(connection, row[0])
            assert record is not None
            records.append(record)
        return tuple(records)

    @staticmethod
    def find(connection: sqlite3.Connection, machine: str, key: list[JsonValue]) -> str | None:
        row = connection.execute(
            "SELECT run_id FROM runs WHERE workflow_id=? AND key=?",
            (machine, canonical_key_json(key)),
        ).fetchone()
        return None if row is None else row["run_id"]

    @staticmethod
    def write(connection: sqlite3.Connection, record: InstanceRecord) -> None:
        connection.execute(
            "UPDATE runs SET key=?, machine_json=? WHERE run_id=?",
            (canonical_key_json(record.key), record.model_dump_json(), record.run_id),
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
    def all_timers(connection: sqlite3.Connection, run_id: str) -> tuple[TimerRecord, ...]:
        rows = connection.execute(
            "SELECT card_json FROM wait_decisions WHERE run_id=? "
            "AND json_type(card_json, '$.machine_timer')='object' ORDER BY position",
            (run_id,),
        )
        return tuple(
            TimerRecord.model_validate(json.loads(row[0])["machine_timer"]) for row in rows
        )

    @staticmethod
    def timers(
        connection: sqlite3.Connection, run_id: str, visit: int
    ) -> tuple[TimerRecord, ...]:
        return tuple(
            sorted(
                (
                    timer
                    for timer in MachinesTable.all_timers(connection, run_id)
                    if timer.visit == visit and timer.reason is None
                ),
                key=lambda timer: (timer.due, timer.timer_index),
            )
        )

    @staticmethod
    def write_timer(connection: sqlite3.Connection, record: TimerRecord) -> None:
        node_id = f"v{record.visit}.after_{record.timer_index}"
        row = connection.execute(
            "SELECT position FROM wait_decisions WHERE run_id=? "
            "AND json_extract(card_json, '$.node_id')=?",
            (record.run_id, node_id),
        ).fetchone()
        position = (
            row[0]
            if row is not None
            else connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM wait_decisions WHERE run_id=?",
                (record.run_id,),
            ).fetchone()[0]
        )
        card = {
            "node_id": node_id,
            "kind": "sleep",
            "state": "waiting"
            if record.reason is None
            else "withdrawn"
            if record.reason == "cancelled"
            else "resolved",
            "schema": {},
            "audit": None,
            "prompt": None,
            "show": None,
            "key": None,
            "until": record.due,
            "remaining_seconds": None,
            "machine_timer": record.model_dump(mode="json"),
        }
        connection.execute(
            "INSERT INTO wait_decisions VALUES (?, ?, ?) "
            "ON CONFLICT(run_id, position) DO UPDATE SET card_json=excluded.card_json",
            (record.run_id, position, journal_json(card)),
        )

    @staticmethod
    def cancel_timers(
        connection: sqlite3.Connection, run_id: str, visit: int | None = None
    ) -> None:
        for timer in MachinesTable.all_timers(connection, run_id):
            if timer.reason is None and (visit is None or timer.visit == visit):
                MachinesTable.write_timer(
                    connection, timer.model_copy(update={"reason": "cancelled"})
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
