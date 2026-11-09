"""Run-owned machine controls shared by gRPC, REST and the local CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from avalanche.machine_host import MachineEventReceipt

from .state.machines import FactRecord, MachinesTable

if TYPE_CHECKING:
    from .operator import Operator


class MachineControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    op: Literal["get", "resolve", "send", "history", "visit", "events", "retry", "fire_next"]
    run_id: str = ""
    machine: str = ""
    key_fields: dict[str, JsonValue] | None = None
    event: str = ""
    payload: JsonValue = None
    idempotency_key: str = ""
    visit: int = Field(default=1, ge=1)
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class MachinePageCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    op: Literal["history", "events"]
    run_id: str
    visit: int
    after: int = Field(ge=0)


class MachineHead(BaseModel):
    machine: str
    key: list[JsonValue]
    run_id: str
    deployment_id: str = ""
    status: str
    state: str
    visit: int
    version: Literal[1] = 1
    accepts: list[str]
    due: float | None
    finished: bool
    machine_spec: JsonValue
    pending_events: list[dict[str, JsonValue]]
    receipts: list[MachineEventReceipt]
    reason: str | None = None
    read_state: Literal["retained", "expired"] = "retained"


def existing_keyed_run(
    op: Operator, selector: str, values: dict[str, JsonValue] | None
) -> str | None:
    descriptor = op._registry.resolve(selector)
    if not descriptor.key_fields:
        return None
    key = op._machine_host.key_fields(descriptor.workflow_id, values or {})
    with op._state_store.transaction() as connection:
        return MachinesTable.find(connection, descriptor.workflow_id, key)


def machine_head(op: Operator, run_id: str) -> MachineHead:
    with op._state_store.transaction() as connection:
        record = MachinesTable.load(connection, run_id)
        if record is None:
            raise KeyError(f"Run {run_id} is not a machine")
        read_state = connection.execute(
            "SELECT read_state FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        timers = MachinesTable.timers(connection, run_id, record.visit)
        inbox = MachinesTable.inbox(connection, run_id)
        receipts = [
            MachineEventReceipt.model_validate_json(row[0])
            for row in connection.execute(
                "SELECT response_json FROM wait_receipts WHERE run_id=? AND scope='machine' "
                "ORDER BY rowid DESC LIMIT 100",
                (run_id,),
            )
        ]
    state = record.spec.states.get(record.state)
    return MachineHead(
        machine=record.machine,
        key=record.key,
        run_id=run_id,
        status=record.run_status.value,
        state=record.state,
        visit=record.visit,
        accepts=[] if state is None else [edge.event for edge in state.transitions],
        due=timers[0].due if timers else None,
        finished=record.phase in {"final", "closed"},
        machine_spec=record.spec.model_dump(mode="json"),
        pending_events=[row.model_dump(mode="json", exclude={"size_bytes"}) for row in inbox],
        receipts=receipts,
        reason=record.reason,
        read_state=read_state,
    )


def control_machine(op: Operator, request: MachineControlRequest) -> JsonValue:
    if request.op == "resolve":
        return op._machine_host.resolve(request.machine, request.key_fields or {})
    # Resolve every operation to the ordinary run before reading its child records.
    if op.get_run(request.run_id) is None:
        raise KeyError(f"Unknown run {request.run_id}")
    with op._state_store.transaction() as connection:
        read_state = connection.execute(
            "SELECT read_state FROM runs WHERE run_id=?", (request.run_id,)
        ).fetchone()[0]
    if read_state == "expired" and request.op in {"history", "events", "visit"}:
        return {"read_state": "expired", "items": [], "next_cursor": None}
    host = op._machine_host
    if request.op == "send":
        if request.machine:
            descriptor = op._registry.resolve(request.machine)
            if descriptor.workflow_id != op.get_run(request.run_id).workflow_id:
                raise ValueError("Run does not belong to the selected machine")
        return host.send(
            request.run_id, request.event, request.payload, request.idempotency_key
        ).model_dump(mode="json")
    if request.op == "retry":
        host.retry(request.run_id)
        return None
    if request.op == "fire_next":
        if not op._machine_dev:
            raise ValueError("Timer fast-forward is available only in ava dev")
        host.fire_next(request.run_id)
        return None
    if request.op == "get":
        return machine_head(op, request.run_id).model_dump(mode="json")
    # Never acquire the operator lock while holding the store transaction lock.
    current_snapshot = (
        op.get_latest_run_snapshot(request.run_id, operator_instance_id=op.operator_instance_id)
        if request.op == "visit"
        else None
    )
    with op._state_store.transaction() as connection:
        if MachinesTable.load(connection, request.run_id) is None:
            raise ValueError("not_machine")
        if request.op == "visit":
            visit = MachinesTable.visit(connection, request.run_id, request.visit)
            snapshot = visit.snapshot or current_snapshot
            assert snapshot is not None
            # The full domain snapshot retains qualified topology, I/O and detail tokens.
            result = visit.model_dump(mode="json", by_alias=True)
            from pydantic import TypeAdapter

            from .models import RunSnapshot

            result["snapshot"] = TypeAdapter(RunSnapshot).dump_python(snapshot, mode="json")
            result["nodes"] = {
                node.node_id: {
                    "state": node.status.value,
                    "kind": node.node_type,
                    "deps": [
                        parent
                        for parent, children in snapshot.topology.graph
                        if node.node_id in children
                    ],
                    "g": 0,
                    "attempts": 1,
                }
                for node in snapshot.nodes
            }
            return result
        offset = 0
        if request.cursor is not None:
            cursor = MachinePageCursor.model_validate_json(request.cursor)
            if (cursor.op, cursor.run_id, cursor.visit) != (
                request.op,
                request.run_id,
                request.visit,
            ):
                raise ValueError("invalid_machine_cursor")
            offset = cursor.after
        if request.op == "history":
            rows = connection.execute(
                "SELECT record_json FROM machine_visits WHERE run_id=? AND visit>? "
                "ORDER BY visit LIMIT ?",
                (request.run_id, offset, request.limit + 1),
            ).fetchall()
            from .state.machines import VisitRecord

            visits = [VisitRecord.model_validate_json(row[0]) for row in rows[: request.limit]]
            items = [
                visit.transition.model_dump(mode="json", by_alias=True)
                for visit in visits
                if visit.transition is not None
            ]
            last = visits[-1].visit if visits else offset
        else:
            rows = connection.execute(
                "SELECT record_json FROM machine_facts WHERE run_id=? AND visit=? AND tseq>? "
                "ORDER BY tseq LIMIT ?",
                (request.run_id, request.visit, offset, request.limit + 1),
            ).fetchall()
            facts = [FactRecord.model_validate_json(row[0]) for row in rows[: request.limit]]
            items = [
                fact.model_dump(mode="json")
                for fact in facts
                if fact.status in {"accepted", "ignored", "consumed", "dropped"}
            ]
            last = facts[-1].tseq if facts else offset
    return {
        "items": items,
        "next_cursor": MachinePageCursor(
            op=request.op, run_id=request.run_id, visit=request.visit, after=last
        ).model_dump_json()
        if len(rows) > request.limit
        else None,
    }
