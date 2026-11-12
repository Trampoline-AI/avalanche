"""Bounded in-process waits for embedded development (not restart durable).

The workflow driver and controls share one condition. Acceptance, deadlines and
cancellation serialize under its lock; waits never occupy an executor worker.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Condition
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, model_validator

from .policies import DecisionTimeoutError, Event, WaitSpec


@dataclass(frozen=True)
class LocalPrincipal:
    """Trusted local/test identity; remote hosts authenticate at their API boundary."""

    name: str = "local"
    permissions: frozenset[str] = frozenset({"event:send", "run:decide"})


class WaitRefusalError(ValueError):
    def __init__(self, code: str, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class WaitReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    receipt_id: str
    run_id: str
    address: str
    principal: str
    accepted_at: datetime
    reason: str = ""
    reviewed: JsonValue = None
    schema_digest: str
    payload_digest: str


class Reconcile(Event):
    kind: Literal["done", "retry", "fail"]
    output: JsonValue = None

    @model_validator(mode="after")
    def done_requires_output(self) -> Reconcile:
        if self.kind == "done" and "output" not in self.model_fields_set:
            raise ValueError("done requires the original step's output")
        if self.kind != "done" and self.output is not None:
            raise ValueError("only done accepts an output")
        return self


@dataclass
class _Wait:
    spec: WaitSpec
    model: type[BaseModel] | None
    deadline: float | None = None
    armed: bool = False
    resolved: bool = False
    value: BaseModel | None = None
    failure: Exception | None = None
    reviewed: JsonValue = None
    output_adapter: TypeAdapter | None = None
    audit: WaitReceipt | None = None


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def reviewed_preview(value: JsonValue) -> JsonValue:
    """Pin large reviewed JSON by digest, with a bounded, explicitly partial preview."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) <= 4096:
        return deepcopy(value)
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
        "preview": encoded[:1024].decode("utf-8", "ignore"),
        "truncated": True,
    }


MAX_FINAL_WAIT_CARD_BYTES = 32 * 1024


def encode_final_wait_card(card: dict[str, JsonValue]) -> str:
    """Use the same bounded export at admission and teardown; never trim an audit."""
    card = deepcopy(card)
    for field_name in ("schema", "show"):
        card[field_name] = reviewed_preview(card[field_name])
    for field_name in ("prompt", "key"):
        value = card[field_name]
        preview = reviewed_preview(value)
        if isinstance(value, str) and isinstance(preview, dict):
            card[field_name] = value[:1024] + f" … [truncated; sha256={preview['sha256']}]"
    # A retained terminal card has no active deadline. This also makes admission
    # independent of the clock's floating-point rendering at later export.
    card["remaining_seconds"] = None
    encoded = json.dumps(card, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_FINAL_WAIT_CARD_BYTES:
        raise WaitRefusalError("wait_card_too_large", 413)
    return encoded


class LocalWaits:
    def __init__(
        self,
        run_id: str,
        declarations: Mapping[str, tuple[WaitSpec, type[BaseModel] | None]],
        *,
        fast_timers: bool = False,
        max_events: int = 64,
        max_payload_bytes: int = 1024 * 1024,
    ) -> None:
        self.run_id = run_id
        self.condition = Condition()
        self.fast_timers = fast_timers
        self.max_events = max_events
        self.max_payload_bytes = max_payload_bytes
        self._waits = {
            node: _Wait(spec.model_copy(deep=True), model)
            for node, (spec, model) in declarations.items()
        }
        self._events = {
            slot.spec.key: node
            for node, slot in self._waits.items()
            if slot.spec.kind == "event"
        }
        self._queue: dict[str, deque[tuple[BaseModel, int]]] = {
            node: deque() for node in self._events.values()
        }
        self._receipts: dict[tuple[str, str], WaitReceipt] = {}
        self._offset = 0.0
        self._closed = False
        self._preloads: dict[str, tuple[BaseModel, LocalPrincipal]] = {}
        self._queued_count = 0
        self._queued_bytes = 0

    def now(self) -> float:
        return time.monotonic() + self._offset

    def notify(self) -> None:
        with self.condition:
            self.condition.notify_all()

    def close(self) -> None:
        with self.condition:
            self._closed = True
            for queued in self._queue.values():
                queued.clear()
            self._queued_count = 0
            self._queued_bytes = 0
            self.condition.notify_all()

    def advance_time(self, duration: timedelta) -> None:
        seconds = duration.total_seconds()
        if seconds < 0:
            raise ValueError("deadline advancement must be nonnegative")
        with self.condition:
            self._offset += seconds
            self._expire()
            self.condition.notify_all()

    def wait_until_waiting(self, timeout: float) -> bool:
        with self.condition:
            return (
                self.condition.wait_for(
                    lambda: self._closed
                    or any(slot.armed and not slot.resolved for slot in self._waits.values()),
                    timeout,
                )
                and not self._closed
            )

    def preload_decision(
        self, node: str, value: BaseModel | dict[str, JsonValue], principal: LocalPrincipal
    ) -> None:
        slot = self._waits.get(node)
        if slot is not None and slot.spec.map_max_items is not None:
            raise WaitRefusalError("map_requires_item_identity", 422)
        if slot is None:
            identity = re.fullmatch(r"(.+)\[(0|[1-9][0-9]*)\]", node)
            if identity is None or identity.group(1) not in self._waits:
                raise WaitRefusalError("unknown_decision", 404)
            slot = self._waits[identity.group(1)]
            if (
                slot.spec.map_max_items is None
                or int(identity.group(2)) >= slot.spec.map_max_items
            ):
                raise WaitRefusalError("unknown_map_item", 404)
        self._authorize(slot, principal, "run:decide")
        if slot.model is None or slot.spec.kind != "decision":
            raise WaitRefusalError("not_a_decision", 422)
        self._preloads[node] = (self._validate(slot, value), principal)

    def expand_decisions(
        self, node: str, items: list[JsonValue], *, arm: bool = True
    ) -> tuple[str, ...]:
        with self.condition:
            parent = self._waits[node]
            maximum = parent.spec.map_max_items
            assert maximum is not None
            if len(items) > maximum:
                raise ValueError(
                    f"map_overflow: {len(items)} items exceeds max_items={maximum}"
                )
            children = tuple(f"{node}[{index}]" for index in range(len(items)))
            if any(
                address.startswith(f"{node}[") and address not in children
                for address in self._preloads
            ):
                raise WaitRefusalError("unknown_map_item", 404)
            # Publish all identities/schemas before any child can accept a decision.
            for child, item in zip(children, items):
                spec = parent.spec.model_copy(
                    update={
                        "map_max_items": None,
                        "map_items": None,
                        "map_source_node_id": None,
                        "show_node_id": None,
                        "show_index": None,
                        "show": deepcopy(item),
                    },
                    deep=True,
                )
                self._waits[child] = _Wait(spec, parent.model)
            if arm:
                for child in children:
                    self.arm(child)
            return children

    def arm_shown_decision(self, node: str, shown: JsonValue) -> None:
        with self.condition:
            slot = self._waits[node]
            slot.spec = slot.spec.model_copy(update={"show": shown}, deep=True)
            self.arm(node)

    def add_hold(self, node: str, output_adapter: TypeAdapter) -> None:
        with self.condition:
            schema: dict[str, JsonValue] = Reconcile.model_json_schema()
            properties = schema["properties"]
            assert isinstance(properties, dict)
            output_schema = output_adapter.json_schema()
            definitions = output_schema.pop("$defs", None)
            properties["output"] = output_schema
            if definitions is not None:
                schema["$defs"] = definitions
            self._waits[node] = _Wait(
                WaitSpec(
                    kind="decision",
                    prompt="Reconcile uncertain outcome",
                    schema=schema,
                ),
                Reconcile,
                output_adapter=output_adapter,
            )

    def arm(self, node: str, reviewed: JsonValue = None) -> None:
        with self.condition:
            slot = self._waits[node]
            if slot.armed:
                return
            slot.armed = True
            slot.reviewed = reviewed_preview(
                reviewed if slot.spec.show_node_id is not None else slot.spec.show
            )
            if slot.spec.until is not None:
                slot.deadline = self.now() + max(
                    0.0, (slot.spec.until - datetime.now(UTC)).total_seconds()
                )
            elif slot.spec.timeout_seconds is not None:
                slot.deadline = self.now() + slot.spec.timeout_seconds
            if slot.spec.kind == "sleep" and self.fast_timers:
                slot.deadline = self.now()
            if slot.spec.kind == "event":
                self._consume(node)
            preload = self._preloads.pop(node, None)
            if preload is not None:
                value, principal = preload
                self.decide(
                    node,
                    value,
                    idempotency_key=f"preload:{node}",
                    principal=principal,
                    reason="preloaded local decision",
                )
            self._expire()
            self.condition.notify_all()

    def _validate(self, slot: _Wait, payload: BaseModel | dict[str, JsonValue]) -> BaseModel:
        assert slot.model is not None
        wire = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        return slot.model.model_validate_json(json.dumps(wire, allow_nan=False), strict=True)

    def _authorize(self, slot: _Wait, principal: LocalPrincipal, permission: str) -> None:
        if not principal.name or permission not in principal.permissions:
            raise WaitRefusalError("unauthorized", 403)
        if permission == "run:decide" and slot.spec.approvers is not None:
            if principal.name not in slot.spec.approvers:
                raise WaitRefusalError("unauthorized", 403)

    def _expire(self) -> None:
        if self._closed:
            return
        now = self.now()
        for node, slot in self._waits.items():
            if not slot.armed or slot.resolved or slot.deadline is None or now < slot.deadline:
                continue
            slot.resolved = True
            if slot.spec.kind == "decision":
                if slot.spec.on_timeout is None:
                    slot.failure = DecisionTimeoutError("decision_timeout")
                else:
                    assert slot.model is not None
                    slot.value = slot.model.model_validate(slot.spec.on_timeout)
                slot.audit = self._receipt(
                    f"decision:{node}",
                    slot.spec.on_timeout,
                    slot.spec.schema_,
                    LocalPrincipal(name="system"),
                    "decision_timeout",
                    slot.reviewed,
                )

    def _consume(self, node: str) -> None:
        slot = self._waits[node]
        if slot.armed and not slot.resolved and self._queue[node]:
            slot.value, size = self._queue[node].popleft()
            self._queued_bytes -= size
            self._queued_count -= 1
            slot.resolved = True

    def _receipt(
        self,
        address: str,
        payload: JsonValue,
        schema: dict[str, JsonValue],
        principal: LocalPrincipal,
        reason: str = "",
        reviewed: JsonValue = None,
    ) -> WaitReceipt:
        return WaitReceipt(
            receipt_id=uuid4().hex,
            run_id=self.run_id,
            address=address,
            principal=principal.name,
            accepted_at=datetime.now(UTC),
            reason=reason,
            reviewed=reviewed_preview(reviewed),
            schema_digest=_digest(schema),
            payload_digest=_digest(payload),
        )

    def _replay(self, address: str, idem: str, payload: JsonValue) -> WaitReceipt | None:
        if not idem:
            raise WaitRefusalError("idempotency_key_required", 422)
        receipt = self._receipts.get((address, idem))
        if receipt is not None and receipt.payload_digest != _digest(payload):
            raise WaitRefusalError("idempotency_conflict", 409)
        return receipt

    def send_event(
        self,
        key: str,
        payload: BaseModel | dict[str, JsonValue],
        *,
        idempotency_key: str,
        principal: LocalPrincipal,
    ) -> WaitReceipt:
        with self.condition:
            if key not in self._events:
                raise WaitRefusalError("unknown_key", 404)
            node = self._events[key]
            slot = self._waits[node]
            self._authorize(slot, principal, "event:send")
            wire = (
                payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
            )
            address = f"event:{key}"
            replay = self._replay(address, idempotency_key, wire)
            if replay is not None:
                return replay.model_copy(deep=True)
            if self._closed:
                raise WaitRefusalError("run_closed", 409)
            value = self._validate(slot, payload)
            size = len(json.dumps(wire, allow_nan=False).encode())
            if (
                self._queued_count >= self.max_events
                or self._queued_bytes + size > self.max_payload_bytes
            ):
                raise WaitRefusalError("inbox_full", 409)
            self._expire()
            receipt = self._receipt(address, wire, slot.spec.schema_, principal)
            self._receipts[(address, idempotency_key)] = receipt
            self._queue[node].append((value, size))
            self._queued_bytes += size
            self._queued_count += 1
            self._consume(node)
            self.condition.notify_all()
            return receipt.model_copy(deep=True)

    def decide(
        self,
        node: str,
        payload: BaseModel | dict[str, JsonValue],
        *,
        idempotency_key: str,
        principal: LocalPrincipal,
        reason: str,
    ) -> WaitReceipt:
        with self.condition:
            slot = self._waits[node]
            if slot.spec.kind != "decision" or slot.spec.map_max_items is not None:
                raise WaitRefusalError("not_a_decision", 422)
            self._authorize(slot, principal, "run:decide")
            if not reason.strip():
                raise WaitRefusalError("reason_required", 422)
            wire = {
                "outcome": (
                    payload.model_dump(mode="json")
                    if isinstance(payload, BaseModel)
                    else payload
                ),
                "reason": reason,
            }
            address = f"decision:{node}"
            replay = self._replay(address, idempotency_key, wire)
            if replay is not None:
                return replay.model_copy(deep=True)
            self._expire()
            if self._closed or slot.resolved:
                raise WaitRefusalError("already_resolved_or_withdrawn", 409)
            if not slot.armed:
                raise WaitRefusalError("not_armed", 409)
            value = self._validate(slot, payload)
            if isinstance(value, Reconcile) and value.kind == "done":
                assert slot.output_adapter is not None
                slot.output_adapter.validate_python(value.output)
            receipt = self._receipt(
                address, wire, slot.spec.schema_, principal, reason, slot.reviewed
            )
            final_card = self._card(node, slot)
            final_card.update(state="resolved", audit=receipt.model_dump(mode="json"))
            encode_final_wait_card(final_card)
            self._receipts[(address, idempotency_key)] = receipt
            slot.value = value
            slot.audit = receipt
            slot.resolved = True
            self.condition.notify_all()
            return receipt.model_copy(deep=True)

    def bind_run(self, node: str, child: str) -> None:
        with self.condition:
            self._waits[node].spec = self._waits[node].spec.model_copy(update={"key": child})
            self.arm(node)

    def finish_run(self, node: str) -> None:
        with self.condition:
            self._waits[node].resolved = True
            self.condition.notify_all()

    def resolved(self, node: str) -> bool:
        self._expire()
        return self._waits[node].resolved

    def result(self, node: str) -> BaseModel | None:
        slot = self._waits[node]
        if not slot.resolved:
            raise RuntimeError("wait is unresolved")
        if slot.failure is not None:
            raise slot.failure
        return slot.value

    def _card(self, node: str, slot: _Wait) -> dict[str, JsonValue]:
        return {
            "node_id": node,
            "kind": slot.spec.kind,
            "state": (
                "withdrawn"
                if self._closed and not slot.resolved
                else "resolved"
                if slot.resolved
                else "waiting"
                if slot.armed
                else "pending"
            ),
            "schema": deepcopy(slot.spec.schema_),
            "audit": slot.audit.model_dump(mode="json") if slot.audit is not None else None,
            "prompt": slot.spec.prompt,
            "show": deepcopy(slot.reviewed),
            "key": slot.spec.key,
            "remaining_seconds": (
                max(0.0, slot.deadline - self.now()) if slot.deadline is not None else None
            ),
        }

    def snapshot(self) -> list[dict[str, JsonValue]]:
        with self.condition:
            self._expire()
            return [
                self._card(node, slot)
                for node, slot in self._waits.items()
                if slot.spec.map_max_items is None
            ]

    def next_deadline(self) -> float | None:
        deadlines = [
            slot.deadline
            for slot in self._waits.values()
            if slot.armed and not slot.resolved and slot.deadline is not None
        ]
        return max(0.0, min(deadlines) - self.now()) if deadlines else None
