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


def existing_machine_run(
    op: Operator, selector: str, values: dict[str, JsonValue] | None
) -> str | None:
    from avalanche.machine_spec import MachineSpec

    descriptor = op._registry.resolve(selector)
    if not descriptor.machine_spec_json:
        return None
    spec = MachineSpec.model_validate_json(descriptor.machine_spec_json)
    key = op._machine_host.key_fields(spec, values or {})
    with op._state_store.transaction() as connection:
        return MachinesTable.find(connection, descriptor.workflow_id, key)


def machine_head(op: Operator, run_id: str) -> MachineHead:
    with op._state_store.transaction() as connection:
        record = MachinesTable.load(connection, run_id)
        if record is None:
            raise KeyError(f"Run {run_id} is not a machine")
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
        status=record.status,
        state=record.state,
        visit=record.visit,
        accepts=[] if state is None else [edge.event for edge in state.transitions],
        due=timers[0].due if timers else None,
        finished=record.status in {"final", "closed"},
        machine_spec=record.spec.model_dump(mode="json"),
        pending_events=[row.model_dump(mode="json", exclude={"size_bytes"}) for row in inbox],
        receipts=receipts,
        reason=record.reason,
    )


def control_machine(op: Operator, request: MachineControlRequest) -> JsonValue:
    if request.op == "resolve":
        return op._machine_host.resolve(request.machine, request.key_fields or {})
    # Resolve every operation to the ordinary run before reading its child records.
    if op.get_run(request.run_id) is None:
        raise KeyError(f"Unknown run {request.run_id}")
    host = op._machine_host
    if request.op == "send":
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
    with op._state_store.transaction() as connection:
        if MachinesTable.load(connection, request.run_id) is None:
            raise ValueError("not_machine")
        if request.op == "visit":
            visit = MachinesTable.visit(connection, request.run_id, request.visit)
            snapshot = visit.snapshot
            if snapshot is None:
                snapshot = op.get_latest_run_snapshot(
                    request.run_id, operator_instance_id=op._operator_instance_id
                )
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
            parts = request.cursor.split(":")
            binding = f"{request.op}:{request.run_id}:{request.visit}"
            if len(parts) != 4 or ":".join(parts[:3]) != binding:
                raise ValueError("invalid_machine_cursor")
            offset = int(parts[3])
            if offset < 0:
                raise ValueError("invalid_machine_cursor")
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
        "next_cursor": f"{request.op}:{request.run_id}:{request.visit}:{last}"
        if len(rows) > request.limit
        else None,
    }
