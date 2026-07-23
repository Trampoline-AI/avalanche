"""gRPC client that implements StateProvider for the TUI."""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Mapping
from numbers import Real
from typing import Any, Callable

import grpc
from pydantic import BaseModel

from avalanche.runtime import File

from ._grpc import _UNLIMITED_MESSAGE_OPTIONS
from .convert import (
    discovery_diagnostic_from_proto,
    run_state_from_proto,
    workflow_info_from_proto,
)
from .models import LogEntry, RunState, WorkflowDiscoveryDiagnostic, WorkflowInfo
from .proto import operator_pb2 as pb
from .proto import operator_pb2_grpc as pb_grpc

DEFAULT_UNARY_TIMEOUT_SECONDS = 10.0
STREAM_THREAD_JOIN_TIMEOUT_SECONDS = 2.0


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
        unary_timeout: float = DEFAULT_UNARY_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(unary_timeout, bool) or not isinstance(unary_timeout, Real):
            raise TypeError("unary_timeout must be a real number")
        if not math.isfinite(unary_timeout) or unary_timeout <= 0:
            raise ValueError("unary_timeout must be positive and finite")

        self._address = address
        self._metadata = (("authorization", f"Bearer {token}"),) if token else None
        self._unary_timeout = float(unary_timeout)
        if tls:
            credentials = grpc.ssl_channel_credentials(
                root_certificates=root_certificates,
                private_key=private_key,
                certificate_chain=certificate_chain,
            )
            self._channel = grpc.secure_channel(
                address,
                credentials,
                options=_UNLIMITED_MESSAGE_OPTIONS,
            )
        else:
            self._channel = grpc.insecure_channel(
                address,
                options=_UNLIMITED_MESSAGE_OPTIONS,
            )
        self._stub = pb_grpc.OperatorServiceStub(self._channel)
        self._run_callbacks: list[Callable[[RunState], None]] = []
        self._log_callbacks: list[Callable[[LogEntry], None]] = []
        self._lifecycle_lock = threading.Lock()
        self._stream_thread: threading.Thread | None = None
        self._stream_stop = threading.Event()
        self._closed = False
        self._last_seq: int = 0
        self._legacy_names_by_workflow_id: dict[str, str] = {}

        # Connection state (read by TUI)
        self.connected: bool = False
        self.retry_count: int = 0
        self.last_error: str = ""
        self.discovery_diagnostics: list[WorkflowDiscoveryDiagnostic] = []

    def _call(self, fn, *args, default=None, **kwargs):
        """Wrap a gRPC call with connection state tracking."""
        kwargs.setdefault("timeout", self._unary_timeout)
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
        self._cache_legacy_workflow_names(resp)
        self.discovery_diagnostics = [
            discovery_diagnostic_from_proto(item) for item in resp.diagnostics
        ]
        return [workflow_info_from_proto(p) for p in resp.flows]

    def list_runs(self, workflow_selector: str) -> list[RunState]:
        legacy_name = self._legacy_names_by_workflow_id.get(
            workflow_selector, workflow_selector
        )
        resp = self._call(
            self._stub.ListRuns,
            pb.ListRunsRequest(
                flow_name=legacy_name,
                workflow_selector=workflow_selector,
            ),
        )
        if resp is None:
            return []
        return [run_state_from_proto(r) for r in resp.runs]

    def get_run(self, run_id: str) -> RunState | None:
        try:
            kwargs = {"timeout": self._unary_timeout}
            if self._metadata is not None:
                kwargs["metadata"] = self._metadata
            resp = self._stub.GetRun(pb.GetRunRequest(run_id=run_id), **kwargs)
            self.connected = True
            self.retry_count = 0
            self.last_error = ""
            return run_state_from_proto(resp)
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                self.connected = True
                self.retry_count = 0
                self.last_error = ""
                return None
            self.connected = False
            self.last_error = f"{e.code().name}: {e.details()}"
            return None

    def start_run(
        self,
        workflow_selector: str,
        *,
        run_id: str | None = None,
        input: Mapping[str, Any] | BaseModel | None = None,
        context: Mapping[str, Any] | BaseModel | None = None,
        files: Mapping[str, File | bytes] | None = None,
    ) -> str:
        input_files = [
            _file_attachment(field_name, value) for field_name, value in (files or {}).items()
        ]
        flow_name = self._legacy_names_by_workflow_id.get(workflow_selector, workflow_selector)
        request = pb.StartRunRequest(
            flow_name=flow_name,
            workflow_selector=workflow_selector,
            run_id=run_id or "",
            input_json=_json_payload(input),
            context_json=_json_payload(context),
            input_files=input_files,
        )
        resp = self._call(self._stub.StartRun, request)
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
        with self._lifecycle_lock:
            if self._closed or self._stream_stop.is_set():
                return
            if self._stream_thread is not None and self._stream_thread.is_alive():
                return
            self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True)
            self._stream_thread.start()

    def ping(self) -> bool:
        """Quick health check — try a fast unary call with short timeout."""
        try:
            kwargs = {"timeout": min(2.0, self._unary_timeout)}
            if self._metadata is not None:
                kwargs["metadata"] = self._metadata
            resp = self._stub.ListFlows(pb.Empty(), **kwargs)
            self._cache_legacy_workflow_names(resp)
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

    def _cache_legacy_workflow_names(self, response: pb.FlowList) -> None:
        self._legacy_names_by_workflow_id = {
            (item.workflow_id or item.name): (item.name or item.display_name)
            for item in response.flows
        }

    def _stream_loop(self) -> None:
        """Background thread: consumes StreamUpdates and fires callbacks."""
        while not self._stream_stop.is_set():
            try:
                self.retry_count += 1
                if self._stream_stop.is_set():
                    break
                stream = self._stub.StreamUpdates(
                    pb.StreamRequest(since_sequence=self._last_seq),
                    metadata=self._metadata,
                )
                with self._lifecycle_lock:
                    if self._closed:
                        break
                    self.connected = True
                    self.retry_count = 0
                    self.last_error = ""
                first_update = True
                for update in stream:
                    if self._stream_stop.is_set():
                        break
                    if first_update:
                        first_update = False
                        if update.sequence < self._last_seq:
                            self._last_seq = 0
                    if update.sequence <= self._last_seq:
                        continue
                    self._last_seq = update.sequence
                    run = run_state_from_proto(update.run)
                    for cb in self._run_callbacks:
                        try:
                            cb(run)
                        except Exception:
                            pass
            except grpc.RpcError as e:
                if self._stream_stop.is_set():
                    break
                self.connected = False
                self.last_error = f"{e.code().name}: {e.details()}"
                delay = min(2 ** min(self.retry_count, 5), 30)
                self._stream_stop.wait(delay)
            except Exception as e:
                if self._stream_stop.is_set():
                    break
                self.connected = False
                self.last_error = str(e)
                self._stream_stop.wait(2.0)

    def close(self) -> None:
        """Stop stream reconnects and close the gRPC channel."""
        with self._lifecycle_lock:
            close_channel = not self._closed
            self._closed = True
            self._stream_stop.set()
            self.connected = False
            thread = self._stream_thread
        if close_channel:
            self._channel.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=STREAM_THREAD_JOIN_TIMEOUT_SECONDS)
        self.connected = False


def _json_payload(payload: Mapping[str, Any] | BaseModel | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, BaseModel):
        return payload.model_dump_json()
    return json.dumps(payload)


def _file_attachment(field_name: str, value: File | bytes) -> pb.FileAttachment:
    file = value if isinstance(value, File) else File(name=field_name, content=value)
    return pb.FileAttachment(
        field_name=field_name,
        name=file.name or "",
        content=file.content,
        content_type=file.content_type or "",
        sha256=file.sha256 or "",
    )
