"""Durable native child links and one terminal-result latch per call node."""

from __future__ import annotations

import sqlite3
import time

from pydantic import BaseModel, ConfigDict

from avalanche.run_steps import RunStart, check_run_wait

from ..machine_steps import MachineStepRequest, MachineStepResponse


class RunLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    parent_run_id: str
    node_id: str
    child_run_id: str
    created: bool


class RunCallsTable:
    @staticmethod
    def start(connection: sqlite3.Connection, parent: str, node: str) -> RunStart | None:
        row = connection.execute(
            "SELECT receipt_json FROM run_links WHERE parent_run_id=? AND node_id=?",
            (parent, node),
        ).fetchone()
        return None if row is None else RunStart.model_validate_json(row[0])

    @staticmethod
    def link(connection: sqlite3.Connection, parent: str, node: str, start: RunStart) -> None:
        connection.execute(
            "INSERT INTO run_links VALUES (?, ?, ?, ?, ?)",
            (parent, node, start.run_id, int(start.created), start.model_dump_json()),
        )

    @staticmethod
    def links(connection: sqlite3.Connection, run_id: str, *, parents: bool) -> list[RunLink]:
        column = "child_run_id" if parents else "parent_run_id"
        rows = connection.execute(
            f"SELECT * FROM run_links WHERE {column}=? ORDER BY parent_run_id, node_id",
            (run_id,),
        ).fetchall()
        return [RunLink(parent_run_id=row["parent_run_id"], node_id=row["node_id"],
                        child_run_id=row["child_run_id"], created=bool(row["created"]))
                for row in rows]

    @staticmethod
    def arm(connection: sqlite3.Connection, request: MachineStepRequest) -> None:
        original = connection.execute(
            "SELECT 1 FROM run_waits WHERE parent_run_id=? AND node_id=?",
            (request.parent_run_id, request.node_id),
        ).fetchone()
        if original is not None:
            return
        rows = connection.execute(
            "SELECT w.parent_run_id, w.child_run_id FROM run_waits w "
            "JOIN runs r ON r.run_id=w.parent_run_id "
            "WHERE w.response_json IS NULL AND r.status NOT IN ('success','failed','cancelled')"
        ).fetchall()
        waiting: dict[str, set[str]] = {}
        for row in rows:
            waiting.setdefault(row[0], set()).add(row[1])
        check_run_wait(request.parent_run_id, request.child_run_id, waiting)
        from avalanche.policies import WaitSpec
        assert isinstance(request.spec, WaitSpec)
        seconds = request.spec.timeout_seconds
        connection.execute(
            "INSERT INTO run_waits VALUES (?, ?, ?, ?, NULL)",
            (request.parent_run_id, request.node_id, request.child_run_id,
             time.time() + seconds if seconds is not None else None),
        )

    @staticmethod
    def latch(
        connection: sqlite3.Connection, parent: str, response: MachineStepResponse
    ) -> None:
        connection.execute(
            "UPDATE run_waits SET response_json=? "
            "WHERE parent_run_id=? AND node_id=? AND response_json IS NULL",
            (response.model_dump_json(), parent, response.node_id),
        )

    @staticmethod
    def parents(connection: sqlite3.Connection) -> set[str]:
        return {row[0] for row in connection.execute(
            "SELECT DISTINCT parent_run_id FROM run_waits"
        ).fetchall()}
