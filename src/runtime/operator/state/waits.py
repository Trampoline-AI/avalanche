"""Terminal wait evidence, shared by ordinary runs and machine visits."""

from __future__ import annotations

import json
import sqlite3
import time

from avalanche.local_waits import WaitRefusalError

from ..wait_control import FinalWaitSnapshot, WaitControlRequest, WaitControlResponse


def request_wire(request: WaitControlRequest) -> str:
    return json.dumps(
        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class WaitsTable:
    @staticmethod
    def finish(
        connection: sqlite3.Connection,
        run_id: str,
        snapshot: FinalWaitSnapshot,
        receipts: dict[tuple[str, str, str], tuple[str, WaitControlResponse]],
    ) -> None:
        offset = connection.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM wait_decisions WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        connection.executemany(
            "INSERT INTO wait_decisions (run_id, position, card_json) VALUES (?, ?, ?)",
            ((run_id, offset + index, card) for index, card in enumerate(snapshot.cards)),
        )
        connection.executemany(
            """INSERT INTO wait_receipts
               (run_id, scope, address, idempotency_key, request_json, response_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                (run_id, kind, address, key, wire, response.model_dump_json())
                for (kind, address, key), (wire, response) in receipts.items()
            ),
        )

    @staticmethod
    def read(
        connection: sqlite3.Connection, run_id: str, request: WaitControlRequest
    ) -> WaitControlResponse:
        if request.kind == "list":
            rows = connection.execute(
                """SELECT card_json FROM wait_decisions WHERE run_id = ?
                   ORDER BY position LIMIT ? OFFSET ?""",
                (run_id, request.limit + 1, request.offset),
            ).fetchall()
            cards = []
            for row in rows[: request.limit]:
                card = json.loads(row["card_json"])
                if "machine_timer" in card:
                    card["remaining_seconds"] = max(0.0, card["until"] - time.time())
                cards.append(
                    json.dumps(card, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                )
            cards = tuple(cards)
            response = FinalWaitSnapshot(cards=cards).read(
                request.model_copy(update={"offset": 0})
            )
            return response.model_copy(
                update={
                    "next_offset": request.offset + request.limit
                    if len(rows) > request.limit
                    else None
                }
            )
        row = connection.execute(
            """SELECT request_json, response_json FROM wait_receipts
               WHERE run_id = ? AND scope = ? AND address = ? AND idempotency_key = ?""",
            (run_id, request.kind, request.address, request.idempotency_key),
        ).fetchone()
        if row is None:
            raise WaitRefusalError("run_closed", 409)
        if row["request_json"] != request_wire(request):
            raise WaitRefusalError("idempotency_conflict", 409)
        return WaitControlResponse.model_validate_json(row["response_json"])
