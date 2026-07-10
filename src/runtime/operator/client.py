"""gRPC client that implements StateProvider for the TUI."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable

import grpc
from pydantic import BaseModel

from avalanche.runtime import (
    MAX_INLINE_FILE_BYTES,
    MAX_INLINE_REQUEST_BYTES,
    File,
    S3File,
)

from .convert import run_state_from_proto, workflow_info_from_proto
from .models import LogEntry, RunState, WorkflowInfo
from .proto import operator_pb2 as pb
from .proto import operator_pb2_grpc as pb_grpc


class GrpcStateProvider:
    """StateProvider backed by a remote gRPC OperatorService.

    Connects to an operator daemon and translates gRPC calls to the
    StateProvider interface that the TUI expects. Tracks connection
    state and retry attempts for the TUI to display.
    """

    def __init__(
        self,
        address: str = "localhost:7433",
        *,
        token: str | None = None,
        tls: bool = False,
        root_certificates: bytes | None = None,
        private_key: bytes | None = None,
        certificate_chain: bytes | None = None,
    ) -> None:
        self._address = address
        self._metadata = (("authorization", f"Bearer {token}"),) if token else None
        if tls:
            credentials = grpc.ssl_channel_credentials(
                root_certificates=root_certificates,
                private_key=private_key,
                certificate_chain=certificate_chain,
            )
            self._channel = grpc.secure_channel(address, credentials)
        else:
            self._channel = grpc.insecure_channel(address)
        self._stub = pb_grpc.OperatorServiceStub(self._channel)
        self._run_callbacks: list[Callable[[RunState], None]] = []
        self._log_callbacks: list[Callable[[LogEntry], None]] = []
        self._stream_thread: threading.Thread | None = None
        self._last_seq: int = 0

        # Connection state (read by TUI)
        self.connected: bool = False
        self.retry_count: int = 0
        self.last_error: str = ""

    def _call(self, fn, *args, default=None, **kwargs):
        """Wrap a gRPC call with connection state tracking."""
        if self._metadata is not None and "metadata" not in kwargs:
            kwargs["metadata"] = self._metadata
        try:
            result = fn(*args, **kwargs)
            if not self.connected:
                self.retry_count = 0
            self.connected = True
            self.last_error = ""
            return result
        except grpc.RpcError as e:
            self.connected = False
            self.retry_count += 1
            self.last_error = f"{e.code().name}: {e.details()}"
            return default

    def list_workflows(self) -> list[WorkflowInfo]:
        resp = self._call(self._stub.ListFlows, pb.Empty())
        if resp is None:
            return []
        return [workflow_info_from_proto(p) for p in resp.flows]

    def list_runs(self, flow_name: str) -> list[RunState]:
        resp = self._call(
            self._stub.ListRuns, pb.ListRunsRequest(flow_name=flow_name)
        )
        if resp is None:
            return []
        return [run_state_from_proto(r) for r in resp.runs]

    def get_run(self, run_id: str) -> RunState | None:
        try:
            kwargs = {}
            if self._metadata is not None:
                kwargs["metadata"] = self._metadata
            resp = self._stub.GetRun(pb.GetRunRequest(run_id=run_id), **kwargs)
            self.connected = True
            self.retry_count = 0
            return run_state_from_proto(resp)
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                return None
            self.connected = False
            self.last_error = f"{e.code().name}: {e.details()}"
            return None

    def start_run(
        self,
        flow_name: str,
        *,
        run_id: str | None = None,
        input: Mapping[str, Any] | BaseModel | None = None,
        context: Mapping[str, Any] | BaseModel | None = None,
        files: Mapping[str, File | bytes] | None = None,
        s3_files: Mapping[str, S3File | str] | None = None,
    ) -> str:
        input_files = [
            _file_attachment(field_name, value)
            for field_name, value in (files or {}).items()
        ]
        _validate_inline_request_size(input_files)
        request = pb.StartRunRequest(
            flow_name=flow_name,
            run_id=run_id or "",
            input_json=_json_payload(input),
            context_json=_json_payload(context),
            input_files=input_files,
            input_s3_files=[
                _s3_file_reference(field_name, value)
                for field_name, value in (s3_files or {}).items()
            ],
        )
        resp = self._call(
            self._stub.StartRun, request
        )
        return resp.run_id if resp else ""

    def cancel_run(self, run_id: str) -> None:
        self._call(self._stub.CancelRun, pb.CancelRunRequest(run_id=run_id))

    def on_run_update(self, callback: Callable[[RunState], None]) -> None:
        self._run_callbacks.append(callback)
        self._ensure_stream()

    def on_log(self, callback: Callable[[LogEntry], None]) -> None:
        self._log_callbacks.append(callback)

    def _ensure_stream(self) -> None:
        """Start the background streaming thread if not already running."""
        if self._stream_thread is not None and self._stream_thread.is_alive():
            return
        self._stream_thread = threading.Thread(
            target=self._stream_loop, daemon=True
        )
        self._stream_thread.start()

    def ping(self) -> bool:
        """Quick health check — try a fast unary call with short timeout."""
        try:
            kwargs = {"timeout": 2.0}
            if self._metadata is not None:
                kwargs["metadata"] = self._metadata
            self._stub.ListFlows(pb.Empty(), **kwargs)
            if not self.connected:
                self.retry_count = 0
            self.connected = True
            self.last_error = ""
            return True
        except grpc.RpcError as e:
            self.connected = False
            self.retry_count += 1
            self.last_error = f"{e.code().name}: {e.details()}"
            return False

    def _stream_loop(self) -> None:
        """Background thread: consumes StreamUpdates and fires callbacks."""
        while True:
            try:
                self.retry_count += 1
                stream = self._stub.StreamUpdates(
                    pb.StreamRequest(since_sequence=self._last_seq),
                    metadata=self._metadata,
                )
                self.connected = True
                self.retry_count = 0
                self.last_error = ""
                for update in stream:
                    self._last_seq = update.sequence
                    run = run_state_from_proto(update.run)
                    for cb in self._run_callbacks:
                        try:
                            cb(run)
                        except Exception:
                            pass
            except grpc.RpcError as e:
                self.connected = False
                self.last_error = f"{e.code().name}: {e.details()}"
                delay = min(2 ** min(self.retry_count, 5), 30)
                time.sleep(delay)
            except Exception as e:
                self.connected = False
                self.last_error = str(e)
                time.sleep(2.0)

    def close(self) -> None:
        """Close the gRPC channel."""
        self._channel.close()


def _json_payload(payload: Mapping[str, Any] | BaseModel | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, BaseModel):
        return payload.model_dump_json()
    return json.dumps(payload)


def _file_attachment(field_name: str, value: File | bytes) -> pb.FileAttachment:
    file = value if isinstance(value, File) else File(name=field_name, content=value)
    if len(file.content) > MAX_INLINE_FILE_BYTES:
        raise ValueError(
            f"File attachment '{field_name}' exceeds the maximum inline file size "
            f"of {MAX_INLINE_FILE_BYTES} bytes. Use ava.S3File for larger files."
        )
    return pb.FileAttachment(
        field_name=field_name,
        name=file.name or "",
        content=file.content,
        content_type=file.content_type or "",
        sha256=file.sha256 or "",
    )


def _validate_inline_request_size(files: list[pb.FileAttachment]) -> None:
    total = sum(len(file.content) for file in files)
    if total > MAX_INLINE_REQUEST_BYTES:
        raise ValueError(
            f"Inline file attachments total {total} bytes, exceeding the maximum "
            f"inline request size of {MAX_INLINE_REQUEST_BYTES} bytes. "
            "Use ava.S3File for larger files."
        )


def _s3_file_reference(field_name: str, value: S3File | str) -> pb.S3FileReference:
    file = value if isinstance(value, S3File) else S3File(uri=value)
    return pb.S3FileReference(
        field_name=field_name,
        uri=file.uri,
        version_id=file.version_id or "",
        etag=file.etag or "",
        size_bytes=file.size_bytes or 0,
        content_type=file.content_type or "",
        sha256=file.sha256 or "",
    )
