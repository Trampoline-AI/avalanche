"""Single-owner machine coordinator: typed ingress, SQLite journal and timer heap.

Only this thread mutates machine records. Coordinators execute one visit, never a
second top-level run. Admission and transitions commit before replies/publication.
"""

from __future__ import annotations

import heapq
import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter

from avalanche.local_waits import WaitRefusalError
from avalanche.machine_core import BufferedEvent, MachineRecord, reduce_machine, retain_inbox
from avalanche.machine_host import MachineEventReceipt
from avalanche.machine_spec import MachineSpec
from avalanche.policies import event
from avalanche.run_steps import RunOpSpec, RunStart

from .machine_steps import HostStepOutput, MachineStepRequest
from .machine_validation import validate_native
from .models import RunState, RunStatus, TriggerKind, WorkflowDescriptor, WorkflowTopology
from .state.machines import (
    InstanceRecord,
    MachinesTable,
    TimerRecord,
    TransitionRecord,
    VisitRecord,
    canonical_json,
)
from .state.runs import RunRequest

if TYPE_CHECKING:
    from .operator import Operator


@dataclass(frozen=True)
class Start:
    selector: str
    request: RunRequest
    run_id: str | None
    trigger: TriggerKind
    reply: Future[str]


@dataclass(frozen=True)
class Send:
    run_id: str
    event: str
    payload: JsonValue
    idempotency_key: str
    reply: Future[MachineEventReceipt]


@dataclass(frozen=True)
class MachineStep:
    request: MachineStepRequest
    reply: Future[HostStepOutput]


@dataclass(frozen=True)
class VisitFinished:
    run_id: str
    visit: int
    status: str
    output: JsonValue
    internal_event: str | None


@dataclass(frozen=True)
class Retry:
    run_id: str
    reply: Future[None]


@dataclass(frozen=True)
class Close:
    run_id: str
    reply: Future[None]


@dataclass(frozen=True)
class FireNextTimer:
    run_id: str
    reply: Future[None]


@dataclass(frozen=True)
class Restart:
    reply: Future[None]


@dataclass(frozen=True)
class Stop:
    reply: Future[None]


Command = (
    Start | Send | MachineStep | VisitFinished | Retry | Close | FireNextTimer | Restart | Stop
)


def _json_payload(value: JsonValue) -> None:
    """Events have no artifact lifetime/ownership protocol: refuse references."""
    canonical_json(value)
    if isinstance(value, dict):
        if (
            value.get("kind") in {"file", "workspace"}
            or value.get("type") in {"file", "workspace"}
            or (value.get("version") == 1 and "entries" in value)
            or "input_files" in value
            or "uploads" in value
        ):
            raise WaitRefusalError("machine_events_require_json_not_file_or_workspace", 422)
        for child in value.values():
            _json_payload(child)
    elif isinstance(value, list):
        for child in value:
            _json_payload(child)


class MachineHost:
    def __init__(self, operator: Operator) -> None:
        self.op = operator
        self.queue: queue.Queue[Command] = queue.Queue()
        self.heap: list[tuple[float, str, int, int]] = []
        self.thread = threading.Thread(target=self._loop, name="avalanche-machine", daemon=True)
        self.thread.start()
        reply: Future[None] = Future()
        self.queue.put(Restart(reply))
        reply.result()

    def start(
        self, selector: str, request: RunRequest, run_id: str | None, trigger: TriggerKind
    ) -> str:
        reply: Future[str] = Future()
        self.queue.put(Start(selector, request, run_id, trigger, reply))
        return reply.result()

    def send(
        self, run_id: str, name: str, payload: JsonValue, idempotency_key: str
    ) -> MachineEventReceipt:
        reply: Future[MachineEventReceipt] = Future()
        self.queue.put(Send(run_id, name, payload, idempotency_key, reply))
        return reply.result()

    @staticmethod
    def validate_send(name: str, payload: JsonValue, idempotency_key: str) -> None:
        if not name or name in {".", ".."} or "/" in name:
            raise ValueError("event must be a nonempty path segment")
        if (
            not idempotency_key
            or len(idempotency_key) > 256
            or not all(char.isprintable() for char in idempotency_key)
        ):
            raise ValueError("idempotency_key must contain 1-256 printable characters")
        _json_payload(payload)

    def retry(self, run_id: str) -> None:
        reply: Future[None] = Future()
        self.queue.put(Retry(run_id, reply))
        reply.result()

    def cancel(self, run_id: str) -> None:
        reply: Future[None] = Future()
        self.queue.put(Close(run_id, reply))
        reply.result()

    def fire_next(self, run_id: str) -> None:
        reply: Future[None] = Future()
        self.queue.put(FireNextTimer(run_id, reply))
        reply.result()

    def close(self) -> None:
        reply: Future[None] = Future()
        self.queue.put(Stop(reply))
        reply.result()
        self.thread.join()

    def _load(self, run_id: str) -> InstanceRecord:
        with self.op._state_store.transaction() as connection:
            record = MachinesTable.load(connection, run_id)
        if record is None:
            raise KeyError(f"Run {run_id} is not a machine instance")
        return record

    def _descriptor(self, selector: str) -> tuple[WorkflowDescriptor, MachineSpec]:
        descriptor = self.op._registry.resolve(selector)
        if not descriptor.machine_spec_json:
            raise ValueError(f"{selector} is not a machine")
        return descriptor, MachineSpec.model_validate_json(descriptor.machine_spec_json)

    def _commit(
        self,
        record: InstanceRecord,
        mutate: Callable[[sqlite3.Connection], None],
        status: RunStatus,
        *,
        request: RunRequest | None = None,
    ) -> None:
        from .operator import _RunStart

        def write(connection: sqlite3.Connection) -> None:
            MachinesTable.write(connection, record)
            mutate(connection)

        with self.op._lock:
            run = self.op._runs[record.run_id]
            run.status = status
            record.run_status = status
            if status in {RunStatus.SUCCESS, RunStatus.CANCELLED, RunStatus.FAILED}:
                run.ended_at = time.monotonic()
            notifications = self.op._publish_run_locked(
                run,
                machine_write=write,
                start=None if request is None else _RunStart(request=request, process=None),
            )
        self.op._wait_for_notifications(notifications)

    def _enter(
        self,
        record: InstanceRecord,
        *,
        request: RunRequest | None = None,
        previous: VisitRecord | None = None,
        transition: TransitionRecord | None = None,
        consumed_receipt: str | None = None,
        fired: TimerRecord | None = None,
    ) -> None:
        descriptor, spec = self._descriptor(record.machine)
        record.spec = spec
        if record.state not in spec.states:
            record.reason = "state_missing"
            self._commit(record, lambda connection: None, RunStatus.WAITING, request=request)
            return
        record.reason = None
        record.outcome = None
        record.internal_event = None
        record.internal_consumed = False
        now = time.time()
        visit = VisitRecord(
            visit=record.visit,
            state=record.state,
            entered_at=now,
            catalog_revision=str(self.op.get_catalog().revision),
        )

        def write(connection: sqlite3.Connection) -> None:
            connection.execute("DELETE FROM run_nodes WHERE run_id=?", (record.run_id,))
            if previous is not None:
                previous.left_at = now
                previous.disposition = "retained"
                previous.transition = transition
                MachinesTable.write_visit(connection, record.run_id, previous)
                MachinesTable.cancel_timers(connection, record.run_id, previous.visit)
            if fired is not None:
                MachinesTable.write_timer(connection, fired)
                MachinesTable.fact(
                    connection,
                    record.run_id,
                    fired.visit,
                    status="timer_fired",
                    source="timer",
                    reason=fired.reason,
                    at=now,
                )
            if consumed_receipt is not None:
                connection.execute(
                    "DELETE FROM wait_inbox WHERE run_id=? AND receipt_id=?",
                    (record.run_id, consumed_receipt),
                )
                MachinesTable.fact(
                    connection,
                    record.run_id,
                    record.visit - 1,
                    status="consumed",
                    receipt_id=consumed_receipt,
                    event=transition.event,
                    at=now,
                )
            inbox = MachinesTable.inbox(connection, record.run_id)
            accepts = [edge.event for edge in spec.states[record.state].transitions]
            # Use the same shared policy; equal payloads must not collapse queue entries.
            buffered = [BufferedEvent(row.event, row.payload, row.size_bytes) for row in inbox]
            retained, _ = retain_inbox(buffered, accepts, spec.states[record.state].defer)
            retained_ids = {id(item) for item in retained}
            for row, item in zip(inbox, buffered, strict=True):
                if id(item) not in retained_ids:
                    connection.execute(
                        "DELETE FROM wait_inbox WHERE run_id=? AND receipt_id=?",
                        (record.run_id, row.receipt_id),
                    )
                    MachinesTable.fact(
                        connection,
                        record.run_id,
                        record.visit,
                        status="dropped",
                        event=row.event,
                        receipt_id=row.receipt_id,
                        at=now,
                    )
            MachinesTable.write_visit(connection, record.run_id, visit)
            for index, timer in enumerate(spec.states[record.state].timers):
                MachinesTable.write_timer(
                    connection,
                    TimerRecord(
                        run_id=record.run_id,
                        visit=record.visit,
                        timer_index=index,
                        due=now + timer.delay_seconds,
                        target=timer.target,
                    ),
                )

        self.heap = [entry for entry in self.heap if entry[1] != record.run_id]
        heapq.heapify(self.heap)
        with self.op._lock:
            run = self.op._runs[record.run_id]
            run.nodes = {}
            run.topology = WorkflowTopology()
            run.expansions = {}
        self._commit(record, write, RunStatus.PENDING, request=request)
        self.op.start_run(record.machine, run_id=record.run_id, _machine_visit=record)

    def resolve(self, selector: str, fields: dict[str, JsonValue]) -> str:
        descriptor = self.op._registry.resolve(selector)
        if not descriptor.key_fields:
            raise ValueError("Workflow does not declare a run key")
        key = self.key_fields(descriptor.workflow_id, fields)
        with self.op._state_store.transaction() as connection:
            existing = MachinesTable.find(connection, descriptor.workflow_id, key)
        if existing is None:
            raise KeyError("Unknown machine key")
        return existing

    def key_fields(self, selector: str, values: dict[str, JsonValue]) -> list[JsonValue]:
        descriptor, root = self.op._registry.resolve_source(selector)
        return TypeAdapter(list[JsonValue]).validate_python(
            validate_native(descriptor, root, "key", values)
        )

    def _start_keyed_flow(self, command: Start) -> str:
        descriptor, root = self.op._registry.resolve_source(command.selector)
        values = command.request.input or {}
        key = self.key_fields(descriptor.workflow_id, values)
        with self.op._state_store.transaction() as connection:
            existing = MachinesTable.find(connection, descriptor.workflow_id, key)
        if existing is not None:
            return existing
        values = TypeAdapter(dict[str, JsonValue]).validate_python(
            validate_native(descriptor, root, "input", values)
        )
        from .run_input_capture import capture_keyed_input

        captured = capture_keyed_input(
            self.op, command.request.model_copy(update={"input": values})
        )
        return self.op.start_run(
            command.selector,
            command.trigger,
            run_id=command.run_id,
            input=captured.input,
            context=captured.context,
            input_files=captured.input_files,
            _key_claim=key,
        )

    def _start(self, command: Start) -> str:
        if not self.op._registry.resolve(command.selector).machine_spec_json:
            return self._start_keyed_flow(command)
        descriptor, spec = self._descriptor(command.selector)
        values = command.request.input or {}
        key = self.key_fields(descriptor.workflow_id, values)
        with self.op._state_store.transaction() as connection:
            existing = MachinesTable.find(connection, descriptor.workflow_id, key)
        if existing is not None:
            return existing
        _, root = self.op._registry.resolve_source(command.selector)
        values = TypeAdapter(dict[str, JsonValue]).validate_python(
            validate_native(descriptor, root, "input", values)
        )
        from .run_input_capture import capture_keyed_input

        captured = capture_keyed_input(
            self.op, command.request.model_copy(update={"input": values})
        )
        command = Start(
            command.selector, captured, command.run_id, command.trigger, command.reply
        )
        run_id = command.run_id or f"run_{uuid4().hex[:8]}"
        with self.op._lock:
            if run_id in self.op._runs:
                raise ValueError(f"Run {run_id} already exists")
            self.op._runs[run_id] = RunState(
                run_id=run_id,
                flow_name=descriptor.display_name,
                workflow_id=descriptor.workflow_id,
                workflow_display_name=descriptor.display_name,
                triggered_at=time.time(),
                triggered_by=command.trigger,
            )
        record = InstanceRecord(
            run_id=run_id,
            machine=descriptor.workflow_id,
            key=key,
            state=spec.initial,
            visit=1,
            spec=spec,
        )
        self._enter(record, request=command.request)
        return run_id

    def _send(self, command: Send) -> MachineEventReceipt:
        self.validate_send(command.event, command.payload, command.idempotency_key)
        record = self._load(command.run_id)
        request_json = canonical_json(command.payload)
        with self.op._state_store.transaction() as connection:
            replay = connection.execute(
                "SELECT request_json, response_json FROM wait_receipts "
                "WHERE run_id=? AND scope='machine' AND address=? AND idempotency_key=?",
                (record.run_id, command.event, command.idempotency_key),
            ).fetchone()
        if replay is not None:
            if replay["request_json"] != request_json:
                raise WaitRefusalError("idempotency_conflict", 409)
            return MachineEventReceipt.model_validate_json(replay["response_json"]).model_copy(
                update={"duplicate": True}
            )
        if record.phase in {"final", "closed"}:
            raise WaitRefusalError("run_closed", 409)
        _, spec = self._descriptor(record.machine)
        record.spec = spec
        if record.state not in spec.states:
            record.reason = "state_missing"
            self._commit(record, lambda connection: None, RunStatus.WAITING)
            raise WaitRefusalError("state_missing", 409)
        state = spec.states[record.state]
        edge = next((edge for edge in state.transitions if edge.event == command.event), None)
        accepted = edge is not None or command.event in state.defer
        if edge is not None:
            if edge.payload_schema is None:
                if command.payload not in ({}, None):
                    raise WaitRefusalError("event_payload_must_be_empty", 422)
            else:
                descriptor, root = self.op._registry.resolve_source(record.machine)
                validate_native(
                    descriptor,
                    root,
                    "event",
                    command.payload,
                    state=record.state,
                    event=command.event,
                )
        receipt = MachineEventReceipt(
            receipt_id=uuid4().hex,
            key=command.idempotency_key,
            accepted_at=time.time(),
            duplicate=False,
            status="accepted" if accepted else "ignored",
        )
        with self.op._state_store.transaction() as connection:
            if accepted:
                rows = MachinesTable.inbox(connection, record.run_id)
                if (
                    len(rows) >= 1000
                    or sum(row.size_bytes for row in rows) + len(request_json.encode())
                    > 1_048_576
                ):
                    raise WaitRefusalError("inbox_full", 409)
                connection.execute(
                    "INSERT INTO wait_inbox VALUES (?, ?, ?, ?, ?)",
                    (
                        record.run_id,
                        receipt.receipt_id,
                        command.event,
                        request_json,
                        len(request_json.encode()),
                    ),
                )
            connection.execute(
                "INSERT INTO wait_receipts VALUES (?, 'machine', ?, ?, ?, ?)",
                (
                    record.run_id,
                    command.event,
                    command.idempotency_key,
                    request_json,
                    receipt.model_dump_json(),
                ),
            )
            MachinesTable.fact(
                connection,
                record.run_id,
                record.visit,
                status=receipt.status,
                event=command.event,
                receipt_id=receipt.receipt_id,
                at=receipt.accepted_at,
            )
        # The receipt is durable before publication or driving work.
        with self.op._lock:
            run_status = self.op._runs[record.run_id].status
        self._commit(record, lambda connection: None, run_status)
        self._drive(record)
        return receipt

    def _drive(
        self, record: InstanceRecord, *, manual: bool = False, catchup: bool = False
    ) -> None:
        if record.phase != "waiting":
            return
        _, spec = self._descriptor(record.machine)
        record.spec = spec
        if record.state not in spec.states:
            record.reason = "state_missing"
            self._commit(record, lambda connection: None, RunStatus.WAITING)
            return
        with self.op._state_store.transaction() as connection:
            timers = MachinesTable.timers(connection, record.run_id, record.visit)
            inbox = MachinesTable.inbox(connection, record.run_id)
            visit = MachinesTable.visit(connection, record.run_id, record.visit)
        now = time.time()
        if manual:
            if not timers:
                raise ValueError("No pending machine timer")
            now = max(now, timers[0].due)
        reduction = reduce_machine(
            spec,
            MachineRecord(record.state, record.payload, record.internal_consumed),
            [BufferedEvent(row.event, row.payload, row.size_bytes) for row in inbox],
            [(timer.due, timer.target) for timer in timers],
            event(record.internal_event, record.outcome)
            if record.internal_event
            else record.outcome,
            now,
            work_done=True,
        )
        if reduction.finished:
            visit.left_at = time.time()
            visit.disposition = "retained"

            def finish(connection: sqlite3.Connection) -> None:
                MachinesTable.write_visit(connection, record.run_id, visit)
                MachinesTable.cancel_timers(connection, record.run_id)

            self._commit(record, finish, RunStatus.SUCCESS)
        elif reduction.transition is not None:
            transition = reduction.transition
            old_visit = record.visit
            chosen = timers[0] if transition.source == "timer" else None
            if chosen is not None:
                chosen.fired_at = time.time()
                chosen.reason = "manual" if manual else "catchup" if catchup else "due"
            receipt_id = (
                None
                if reduction.consumed_index is None
                else inbox[reduction.consumed_index].receipt_id
            )
            record.state = reduction.record.state
            record.payload = reduction.record.payload
            record.visit += 1
            self._enter(
                record,
                previous=visit,
                consumed_receipt=receipt_id,
                fired=chosen,
                transition=TransitionRecord(
                    **{
                        "from": visit.state,
                        "to": record.state,
                        "event": transition.event,
                        "source": transition.source,
                        "visit": old_visit,
                    }
                ),
            )
        else:
            record.internal_consumed = reduction.record.internal_consumed
            self._commit(record, lambda connection: None, RunStatus.WAITING)
            for timer in timers:
                entry = (timer.due, record.run_id, record.visit, timer.timer_index)
                if entry not in self.heap:
                    heapq.heappush(self.heap, entry)

    def _finished(self, command: VisitFinished) -> None:
        record = self._load(command.run_id)
        if record.visit != command.visit or record.phase != "active":
            return
        with self.op._state_store.transaction() as connection:
            visit = MachinesTable.visit(connection, record.run_id, record.visit)
        visit.snapshot = self.op.get_latest_run_snapshot(
            record.run_id, operator_instance_id=self.op._operator_instance_id
        )
        if command.status == "success":
            record.outcome, record.internal_event = command.output, command.internal_event
            self._commit(
                record,
                lambda connection: MachinesTable.write_visit(connection, record.run_id, visit),
                RunStatus.WAITING,
            )
            self._drive(record)
        else:
            record.reason = "outcome_unknown"
            visit.disposition = "indeterminate"
            self._commit(
                record,
                lambda connection: MachinesTable.write_visit(connection, record.run_id, visit),
                RunStatus.WAITING,
            )

    def _restart(self) -> None:
        with self.op._state_store.transaction() as connection:
            records = MachinesTable.all(connection)

        def next_due(record: InstanceRecord) -> float:
            with self.op._state_store.transaction() as connection:
                timers = MachinesTable.timers(connection, record.run_id, record.visit)
            return timers[0].due if timers else float("inf")

        for record in sorted(records, key=next_due):
            if record.phase == "active":
                record.reason = "outcome_unknown"
                with self.op._state_store.transaction() as connection:
                    visit = MachinesTable.visit(connection, record.run_id, record.visit)
                visit.disposition = "indeterminate"
                visit.snapshot = self.op.get_latest_run_snapshot(
                    record.run_id, operator_instance_id=self.op.operator_instance_id
                )
                self._commit(
                    record,
                    lambda connection: MachinesTable.write_visit(
                        connection, record.run_id, visit
                    ),
                    RunStatus.WAITING,
                )
            elif record.phase == "waiting":
                self._drive(record, catchup=True)

    def _step(self, request: MachineStepRequest) -> HostStepOutput:
        if not request.enabled:
            return None
        if isinstance(request.spec, RunOpSpec):
            import hashlib

            descriptor = self.op._registry.resolve(request.target)
            values = request.input or {}
            key = (
                self.key_fields(descriptor.workflow_id, values)
                if descriptor.key_fields
                else None
            )
            with self.op._state_store.transaction() as connection:
                existing = (
                    MachinesTable.find(connection, descriptor.workflow_id, key)
                    if key is not None
                    else None
                )
            proposed = (
                "run_" + hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:32]
            )
            existing = existing or (proposed if self.op.get_run(proposed) is not None else None)
            if existing is not None:
                run_id = existing
            elif descriptor.machine_spec_json or descriptor.key_fields:
                run_id = self._start(
                    Start(
                        request.target,
                        RunRequest(input=values),
                        proposed,
                        TriggerKind.API,
                        Future(),
                    )
                )
            else:
                run_id = self.op.start_run(
                    request.target, TriggerKind.API, input=values, run_id=proposed
                )
            with self.op._state_store.transaction() as connection:
                read_state = connection.execute(
                    "SELECT read_state FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            return RunStart(
                run_id=run_id, key=key, created=existing is None, read_state=read_state
            )
        descriptor, spec = self._descriptor(request.spec.machine)
        if isinstance(request.key, str):
            run_id = request.key
            record = self._load(run_id)
            if record.machine != descriptor.workflow_id:
                raise ValueError("Run does not belong to the selected machine")
        else:
            if request.key is None or len(request.key) != len(spec.key_fields):
                raise ValueError("Machine key has the wrong number of fields")
            run_id = self.resolve(
                request.spec.machine, dict(zip(spec.key_fields, request.key, strict=True))
            )
        return self._send(
            Send(run_id, request.event, request.payload, request.idempotency_key, Future())
        )

    def _loop(self) -> None:
        while True:
            timeout = None if not self.heap else max(0, self.heap[0][0] - time.time())
            try:
                command = self.queue.get(timeout=timeout)
            except queue.Empty:
                _, run_id, visit, index = heapq.heappop(self.heap)
                try:
                    record = self._load(run_id)
                    if record.phase == "waiting" and record.visit == visit:
                        with self.op._state_store.transaction() as connection:
                            timers = MachinesTable.timers(connection, run_id, visit)
                        if any(timer.timer_index == index for timer in timers):
                            self._drive(record)
                except Exception as exc:
                    self.op._record_fatal_error(exc)
                continue
            try:
                match command:
                    case Stop(reply=reply):
                        reply.set_result(None)
                        return
                    case Start():
                        command.reply.set_result(self._start(command))
                    case Send():
                        command.reply.set_result(self._send(command))
                    case MachineStep(request=request, reply=reply):
                        reply.set_result(self._step(request))
                    case VisitFinished():
                        self._finished(command)
                    case Restart(reply=reply):
                        self._restart()
                        reply.set_result(None)
                    case Retry(run_id=run_id, reply=reply):
                        record = self._load(run_id)
                        if record.phase != "interrupted":
                            raise ValueError("Only an interrupted machine visit can be retried")
                        with self.op._state_store.transaction() as connection:
                            previous = MachinesTable.visit(connection, run_id, record.visit)
                        record.visit += 1
                        self._enter(record, previous=previous)
                        reply.set_result(None)
                    case Close(run_id=run_id, reply=reply):
                        record = self._load(run_id)
                        if record.phase not in {"closed", "final"}:

                            def cancel(connection: sqlite3.Connection) -> None:
                                MachinesTable.cancel_timers(connection, run_id)

                            self._commit(record, cancel, RunStatus.CANCELLED)
                        reply.set_result(None)
                    case FireNextTimer(run_id=run_id, reply=reply):
                        self._drive(self._load(run_id), manual=True)
                        reply.set_result(None)
            except Exception as exc:
                if isinstance(command, VisitFinished):
                    self.op._record_fatal_error(exc)
                else:
                    command.reply.set_exception(exc)
