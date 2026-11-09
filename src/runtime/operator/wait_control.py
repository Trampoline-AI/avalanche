"""Local coordinator control protocol; caller identity is never in a request body."""

from __future__ import annotations

import getpass
import json
from dataclasses import dataclass, field
from multiprocessing.connection import Connection, wait
from threading import Lock
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from avalanche.local_waits import (
    MAX_FINAL_WAIT_CARD_BYTES,
    LocalPrincipal,
    WaitReceipt,
    WaitRefusalError,
    encode_final_wait_card,
)
from avalanche.run_handle import RunHandle

MAX_CONTROL_BYTES = 2 * 1024 * 1024


class WaitControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["event", "decision", "list", "cancel"]
    address: str = ""
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    idempotency_key: str = ""
    reason: str = Field(default="", max_length=4096)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=64, ge=1, le=64)


class WaitControlResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    receipt: WaitReceipt | None = None
    waits: list[dict[str, JsonValue]] = Field(default_factory=list)
    error: str | None = None
    status: int = 200
    next_offset: int | None = None


def encode_response(response: WaitControlResponse) -> bytes:
    """Refuse before writing a frame: recv_bytes' limit errors poison a pipe."""
    wire = response.model_dump_json().encode()
    if len(wire) > MAX_CONTROL_BYTES:
        return (
            WaitControlResponse(error="response_too_large", status=413)
            .model_dump_json()
            .encode()
        )
    return wire


def list_response(
    cards: list[dict[str, JsonValue]], request: WaitControlRequest
) -> WaitControlResponse:
    end = request.offset + request.limit
    return WaitControlResponse(
        waits=cards[request.offset : end],
        next_offset=end if end < len(cards) else None,
    )


class FinalWaitSnapshot(BaseModel):
    """Immutable per-card JSON, bounded metadata; cardinality follows the pinned graph.

    Keep audit identities intact and preview only large display fields. Reads page
    these records rather than duplicating all artifacts in a single control frame.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    # Operator admission caps graph nodes at 10,000; each decision map adds
    # at most 64 children. Display fields are previewed before serialization.
    cards: tuple[Annotated[str, Field(max_length=MAX_FINAL_WAIT_CARD_BYTES)], ...] = Field(
        default=(), max_length=640_000
    )

    @classmethod
    def capture(cls, handle: RunHandle) -> FinalWaitSnapshot:
        return cls(
            cards=tuple(encode_final_wait_card(card) for card in handle.waits.snapshot())
        )

    def read(self, request: WaitControlRequest) -> WaitControlResponse:
        end = request.offset + request.limit
        # Pydantic owns the JSON boundary, including the detached nested values.
        return WaitControlResponse.model_validate_json(
            json.dumps(
                {
                    "waits": [json.loads(card) for card in self.cards[request.offset : end]],
                    "next_offset": end if end < len(self.cards) else None,
                }
            )
        )


@dataclass
class RunControls:
    connection: Connection
    machine_writer: Connection | None = None
    lock: Lock = field(default_factory=Lock)
    receipts: dict[tuple[str, str, str], tuple[str, WaitControlResponse]] = field(
        default_factory=dict
    )

    final_snapshot: FinalWaitSnapshot | None = None

    def close(self) -> None:
        with self.lock:
            self.connection.close()
            if self.machine_writer is not None:
                self.machine_writer.close()

    def request(
        self, request: WaitControlRequest, *, closed: bool = False
    ) -> WaitControlResponse:
        identity = (request.kind, request.address, request.idempotency_key)
        wire = json.dumps(
            request.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self.lock:
            original = self.receipts.get(identity)
            if original is not None:
                old_wire, response = original
                if old_wire != wire:
                    raise WaitRefusalError("idempotency_conflict", 409)
                return response.model_copy(deep=True)
            if request.kind == "list" and self.final_snapshot is not None:
                response = WaitControlResponse.model_validate_json(
                    encode_response(self.final_snapshot.read(request))
                )
                if response.error is not None:
                    raise WaitRefusalError(response.error, response.status)
                return response
            if closed:
                raise WaitRefusalError("run_closed", 409)
            if len(wire.encode()) > MAX_CONTROL_BYTES:
                raise WaitRefusalError("request_too_large", 413)
            try:
                self.connection.send_bytes(wire.encode())
                if not self.connection.poll(15):
                    self.connection.close()
                    raise WaitRefusalError("coordinator_unavailable", 503)
                response = WaitControlResponse.model_validate_json(
                    self.connection.recv_bytes(MAX_CONTROL_BYTES)
                )
            except (EOFError, OSError) as exc:
                raise WaitRefusalError("coordinator_unavailable", 503) from exc
            if response.error is not None:
                raise WaitRefusalError(response.error, response.status)
            if response.receipt is not None:
                self.receipts[identity] = (wire, response)
            return response.model_copy(deep=True)


def serve_wait_controls(
    handle: RunHandle,
    connection: Connection,
    cancel_reader: Connection | None = None,
    machine_reader: Connection | None = None,
    machine_pending=None,
) -> None:
    """Use the coordinator's existing main thread; one wake pipe per run, not per wait."""
    from multiprocessing import Pipe

    done_reader, done_writer = Pipe(duplex=False)

    def wake_done(_future) -> None:
        try:
            done_writer.send_bytes(b"done")
        except (BrokenPipeError, OSError):
            pass  # Coordinator teardown may already have withdrawn the wake pipe.

    handle._future.add_done_callback(wake_done)
    principal = LocalPrincipal(name=getpass.getuser())
    try:
        while not handle.done():
            ready = wait(
                [connection, done_reader]
                + ([cancel_reader] if cancel_reader is not None else [])
                + ([machine_reader] if machine_reader is not None else [])
            )
            if machine_reader is not None and machine_reader in ready:
                from .machine_steps import MachineStepResponse

                machine_pending.resolve(
                    MachineStepResponse.model_validate_json(
                        machine_reader.recv_bytes(MAX_CONTROL_BYTES)
                    )
                )
                continue
            if cancel_reader is not None and cancel_reader in ready:
                handle.cancel()
                break
            if connection not in ready:
                break
            request = WaitControlRequest.model_validate_json(
                connection.recv_bytes(MAX_CONTROL_BYTES)
            )
            try:
                if request.kind == "cancel":
                    handle.cancel()
                    response = WaitControlResponse()
                elif request.kind == "list":
                    response = list_response(handle.waits.snapshot(), request)
                elif request.kind == "event":
                    response = WaitControlResponse(
                        receipt=handle.send_event(
                            request.address,
                            request.payload,
                            idempotency_key=request.idempotency_key,
                            principal=principal,
                        )
                    )
                else:
                    response = WaitControlResponse(
                        receipt=handle.decide(
                            request.address,
                            request.payload,
                            idempotency_key=request.idempotency_key,
                            reason=request.reason,
                            principal=principal,
                        )
                    )
            except WaitRefusalError as exc:
                response = WaitControlResponse(error=exc.code, status=exc.status)
            except (ValueError, KeyError):
                response = WaitControlResponse(error="invalid_wait_submission", status=422)
            connection.send_bytes(encode_response(response))
    finally:
        done_reader.close()
        done_writer.close()
        connection.close()
        if cancel_reader is not None:
            cancel_reader.close()
