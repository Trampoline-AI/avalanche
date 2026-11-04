"""Local coordinator control protocol; caller identity is never in a request body."""

from __future__ import annotations

import getpass
import json
from dataclasses import dataclass, field
from multiprocessing.connection import Connection, wait
from threading import Lock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from avalanche.local_waits import LocalPrincipal, WaitReceipt, WaitRefusalError
from avalanche.run_handle import RunHandle


class WaitControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["event", "decision", "list", "cancel"]
    address: str = ""
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    idempotency_key: str = ""
    reason: str = ""


class WaitControlResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    receipt: WaitReceipt | None = None
    waits: list[dict[str, JsonValue]] = Field(default_factory=list)
    error: str | None = None
    status: int = 200


@dataclass
class RunControls:
    connection: Connection
    lock: Lock = field(default_factory=Lock)
    receipts: dict[tuple[str, str, str], tuple[str, WaitControlResponse]] = field(
        default_factory=dict
    )

    def close(self) -> None:
        with self.lock:
            self.connection.close()

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
            if closed:
                raise WaitRefusalError("run_closed", 409)
            try:
                self.connection.send_bytes(wire.encode())
                if not self.connection.poll(15):
                    self.connection.close()
                    raise WaitRefusalError("coordinator_unavailable", 503)
                response = WaitControlResponse.model_validate_json(
                    self.connection.recv_bytes(2 * 1024 * 1024)
                )
            except (EOFError, OSError) as exc:
                raise WaitRefusalError("coordinator_unavailable", 503) from exc
            if response.error is not None:
                raise WaitRefusalError(response.error, response.status)
            if response.receipt is not None:
                self.receipts[identity] = (wire, response)
            return response.model_copy(deep=True)


def serve_wait_controls(
    handle: RunHandle, connection: Connection, cancel_reader: Connection | None = None
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
            )
            if cancel_reader is not None and cancel_reader in ready:
                handle.cancel()
                break
            if connection not in ready:
                break
            request = WaitControlRequest.model_validate_json(
                connection.recv_bytes(2 * 1024 * 1024)
            )
            try:
                if request.kind == "cancel":
                    handle.cancel()
                    response = WaitControlResponse()
                elif request.kind == "list":
                    response = WaitControlResponse(waits=handle.waits.snapshot())
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
            connection.send_bytes(response.model_dump_json().encode())
    finally:
        done_reader.close()
        done_writer.close()
        connection.close()
        if cancel_reader is not None:
            cancel_reader.close()
