"""Machine inbox policy on the existing local run-control boundary."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from copy import deepcopy

from pydantic import BaseModel, JsonValue

from .local_waits import LocalPrincipal, LocalWaits, WaitReceipt, WaitRefusalError
from .machine_core import BufferedEvent, InboxFact, consumable_index, retain_inbox
from .policies import WaitSpec
from .references import LocalReferenceBinder, bind_references


def _event_references(value: JsonValue) -> set[str]:
    from .file import File
    from .references import LocalWorkspaceStore
    from .workspace import Workspace

    references: set[str] = set()
    if isinstance(value, list):
        for item in value:
            references.update(_event_references(item))
    elif isinstance(value, dict):
        if value.get("kind") == "file" and "ref" in value:
            file = File.model_validate(value)
            file.bind(LocalReferenceBinder().bind_file(file)).snapshot()
            references.add(file.ref.workspace)
        elif value.get("kind") == "workspace":
            workspace = Workspace.model_validate(value)
            if workspace.reference is None:
                raise ValueError("Event Workspace requires a commit reference")
            LocalWorkspaceStore.resolve(workspace.reference)
            references.add(workspace.reference)
        else:
            for item in value.values():
                references.update(_event_references(item))
    return references


class MachineWaits(LocalWaits):
    """Fake-clock inbox; no driver thread, sleeps or executor workers."""

    def __init__(self, run_id: str) -> None:
        super().__init__(run_id, {})
        self.accepts: Mapping[str, type[BaseModel] | None] = {}
        self.defer: tuple[str, ...] = ()
        self.buffer: deque[BufferedEvent] = deque()
        self.facts: list[InboxFact] = []
        self.reference_grants: set[str] = set()

    def now(self) -> float:
        return self._offset

    def enter(
        self,
        accepts: Mapping[str, type[BaseModel] | None],
        defer: tuple[str, ...],
    ) -> None:
        self.accepts, self.defer = accepts, defer
        retained, dropped = retain_inbox(tuple(self.buffer), accepts, defer)
        self.facts.extend(dropped)
        self._queued_count -= len(self.buffer) - len(retained)
        self._queued_bytes -= sum(event.size for event in self.buffer) - sum(
            event.size for event in retained
        )
        self.buffer = deque(retained)

    def send_event(
        self,
        key: str,
        payload: BaseModel | dict[str, JsonValue],
        *,
        idempotency_key: str,
        principal: LocalPrincipal,
    ) -> WaitReceipt:
        with self.condition:
            if not principal.name or "event:send" not in principal.permissions:
                raise WaitRefusalError("unauthorized", 403)
            wire = (
                payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
            )
            address = f"event:{key}"
            replay = self._replay(address, idempotency_key, wire)
            if replay is not None:
                return replay
            if self._closed:
                raise WaitRefusalError("run_closed", 409)
            accepted = key in self.accepts or key in self.defer
            model = self.accepts.get(key)
            if key in self.accepts:
                if model is None and wire != {}:
                    raise WaitRefusalError("event_payload_must_be_empty", 422)
                value = (
                    model.model_validate_json(json.dumps(wire, allow_nan=False), strict=True)
                    if model is not None
                    else None
                )
            else:
                value = deepcopy(wire) if key in self.defer else None
            schema = model.model_json_schema() if model is not None else {}
            reason = "accepted" if accepted else "ignored"
            receipt = self._receipt(address, wire, schema, principal, reason)
            size = len(json.dumps(wire, allow_nan=False).encode())
            if accepted:
                if (
                    self._queued_count >= self.max_events
                    or self._queued_bytes + size > self.max_payload_bytes
                ):
                    raise WaitRefusalError("inbox_full", 409)
                # Check all refs, including deferred payloads, before accepting
                # the receipt. This host has one local-OS project/principal.
                self.reference_grants.update(_event_references(wire))
                self.buffer.append(BufferedEvent(key, value, size))
                self._queued_count += 1
                self._queued_bytes += size
            else:
                self.facts.append(InboxFact(event=key, kind="event_ignored"))
            self._receipts[(address, idempotency_key)] = receipt
            return receipt

    def consume(self) -> BufferedEvent | None:
        index = consumable_index(tuple(self.buffer), self.accepts)
        if index is not None:
            event = self.buffer[index]
            self.buffer.remove(event)
            self._queued_count -= 1
            self._queued_bytes -= event.size
            model = self.accepts[event.name]
            wire = (
                event.payload.model_dump(mode="json")
                if isinstance(event.payload, BaseModel)
                else event.payload or {}
            )
            if model is None and wire != {}:
                raise WaitRefusalError("event_payload_must_be_empty", 422)
            payload = (
                (
                    event.payload
                    if isinstance(event.payload, model)
                    else model.model_validate_json(
                        json.dumps(wire, allow_nan=False), strict=True
                    )
                )
                if model is not None
                else None
            )
            return BufferedEvent(
                event.name, bind_references(payload, LocalReferenceBinder()), event.size
            )
        return None

    def add_visit_waits(
        self,
        declarations: Mapping[str, tuple[WaitSpec, type[BaseModel] | None]],
    ) -> None:
        # LocalWaits owns decision/sleep validation and fake-clock resolution.
        visit = LocalWaits(self.run_id, declarations)
        self._waits = visit._waits
        self._events = visit._events
        self._queue = visit._queue

    def close(self) -> None:
        self.buffer.clear()
        super().close()
