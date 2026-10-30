"""TuiExtensions v1 over the local operator's bounded resources.

Start uses a deterministic caller-owned run ID for each intent. The operator
rejects reuse of an existing ID; after an uncertain response keep the same
intent (never silently retry with a new ID). Cancel is an idempotent request,
not a promise that the worker has already stopped. A workflow with a cron
schedule also offers enable or disable; that state lives only in the operator's
memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

import grpc
from pydantic import JsonValue

from avalanche.file import File
from tui.state import (
    JSON,
    SCHEDULE_ACTION_IDS,
    ActionDescriptor,
    ActionResult,
    ExtensionDescription,
    ExtensionPage,
    Invalidation,
    Object,
    PanelDescriptor,
    ScopeDescriptor,
    TuiContext,
    TuiExtensionError,
    json_plain,
    schedule_action,
    schedule_input_schema,
    schedule_request,
)

from .client import OperatorCallError
from .models import RunStatus, TriggerKind
from .proto import operator_pb2 as pb

if TYPE_CHECKING:
    from .client import GrpcStateProvider


@dataclass(frozen=True)
class _ArtifactReference:
    run_id: str
    artifact_id: str
    revision: str
    file: File

    @property
    def size_bytes(self) -> int:
        return self.file.size

    @property
    def sha256(self) -> str:
        assert self.file.sha256 is not None
        return self.file.sha256


def _freeze(value: JsonValue) -> JSON:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _object(value: dict[str, JsonValue]) -> Object:
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})


class OperatorTuiExtensions:
    protocol_version = (1, 0)
    features = frozenset({"paged-resources", "json-schema-forms", "invalidations"})

    def __init__(self, provider: GrpcStateProvider) -> None:
        self._provider = provider
        self._lock = threading.RLock()
        self._callbacks: list[Callable[[Invalidation], None]] = []
        self._context = TuiContext(())
        self._closed = False
        self._artifacts: OrderedDict[tuple[str, str], _ArtifactReference] = OrderedDict()
        self._intents: OrderedDict[str, tuple[str, str]] = OrderedDict()

    def _select(self, context: TuiContext) -> None:
        if context.scope not in ((), (("operator", self._provider.connection_label),)):
            raise TuiExtensionError("not_found", "Unknown operator scope", context=context)
        with self._lock:
            if self._closed:
                raise TuiExtensionError("unavailable", "Provider is closed", context=context)
            if context.generation >= self._context.generation:
                self._context = context

    def describe(self, context: TuiContext) -> ExtensionDescription:
        self._select(context)
        provider = self._provider
        try:
            with provider._state_lock:
                catalog = provider._catalog
            if not catalog.operator_instance_id:
                catalog = provider.get_catalog()
            workflow = next(
                (flow for flow in catalog.workflows if flow.selector == context.flow), None
            )
            actions: list[ActionDescriptor] = []
            if workflow is not None and workflow.input_schema_json:
                if context.version is not None and context.version != workflow.version:
                    raise TuiExtensionError(
                        "conflict", "Workflow version changed", context=context
                    )
                schema = json.loads(workflow.input_schema_json)
                actions.append(
                    ActionDescriptor(
                        "run.start",
                        "Start run",
                        None,
                        not provider._read_only,
                        "read_only" if provider._read_only else None,
                        True,
                        "Start this workflow with the previewed input?",
                        _object(schema),
                    )
                )
            if workflow is not None and workflow.cron:
                actions.append(
                    schedule_action(
                        workflow.schedule_enabled,
                        provider._read_only,
                        schedule_input_schema(workflow),
                    )
                )
            if context.run is not None:
                snapshot = provider.get_latest_run_snapshot(
                    context.run, catalog.operator_instance_id
                )
                if context.flow is not None and snapshot.summary.workflow_id != context.flow:
                    raise TuiExtensionError(
                        "not_found", "Run does not belong to selected flow", context=context
                    )
                active = snapshot.summary.status not in {
                    RunStatus.SUCCESS,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                }
                actions.append(
                    ActionDescriptor(
                        "run.cancel",
                        "Cancel run",
                        None,
                        active and not provider._read_only,
                        "read_only"
                        if provider._read_only
                        else (None if active else "run_terminal"),
                        True,
                        "Request cancellation of this run?",
                        None,
                    )
                )
                if snapshot.summary.status == RunStatus.SUCCESS:
                    actions.append(
                        ActionDescriptor(
                            "artifact.download",
                            "Download artifact",
                            None,
                            True,
                            None,
                            False,
                            "Write the verified artifact to this destination (no overwrite)?",
                            _object(
                                {
                                    "type": "object",
                                    "properties": {
                                        "artifact_id": {"type": "string", "minLength": 1},
                                        "destination": {"type": "string", "minLength": 1},
                                    },
                                    "required": ["artifact_id", "destination"],
                                    "additionalProperties": False,
                                }
                            ),
                        )
                    )
            panels = [
                PanelDescriptor(
                    "runs",
                    "Run history",
                    "flow",
                    "runs",
                    "table",
                    ("id", "status", "triggered_at"),
                ),
                PanelDescriptor(
                    "schedules",
                    "Schedules",
                    "tools",
                    "schedules",
                    "table",
                    ("id", "enabled", "cron", "input", "next_run_at", "last_run_at"),
                    actions=SCHEDULE_ACTION_IDS,
                ),
            ]
            if context.run is not None:
                panels.append(
                    PanelDescriptor(
                        "artifacts",
                        "Artifacts",
                        "run",
                        "artifacts",
                        "table",
                        ("id", "name", "bytes", "sha256"),
                    )
                )
            return ExtensionDescription(
                context,
                self.protocol_version,
                self.features,
                f"{catalog.operator_instance_id}:catalog:{catalog.revision}",
                (
                    ScopeDescriptor(
                        "operator",
                        provider.connection_label,
                        TuiContext((("operator", provider.connection_label),)),
                        True,
                        None,
                    ),
                ),
                tuple(actions),
                tuple(panels),
            )
        except OperatorCallError as error:
            raise self._error(error, context) from error

    def read(
        self,
        resource: str,
        context: TuiContext,
        *,
        cursor: str | None = None,
        limit: int = 100,
        byte_limit: int = 262144,
    ) -> ExtensionPage:
        self._select(context)
        if not 1 <= limit <= 500 or not 1 <= byte_limit <= 1_048_576:
            raise TuiExtensionError("invalid_input", "Invalid page bounds", context=context)
        try:
            response = self._provider._call(
                self._provider._stub.ReadTuiResource,
                pb.ReadTuiResourceRequestV2(
                    resource=resource,
                    workflow_selector=context.flow or "",
                    run_id=context.run or "",
                    version=context.version or "",
                    node_id=context.node or "",
                    attempt=context.attempt or "",
                    cursor=cursor or "",
                    limit=limit,
                    byte_limit=byte_limit,
                ),
            )
        except OperatorCallError as error:
            raise self._error(error, context) from error
        if (
            len(response.items_json) > limit
            or sum(len(row.encode()) for row in response.items_json)
            + max(0, len(response.items_json) - 1)
            + 2
            > byte_limit
        ):
            raise TuiExtensionError(
                "integrity", "Resource page exceeded its bounds", context=context
            )
        rows = []
        for encoded in response.items_json:
            row = json.loads(encoded)
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise TuiExtensionError("integrity", "Invalid resource row", context=context)
            if resource == "artifacts" and row.get("kind") == "file":
                file = File.model_validate(row["reference"])
                reference = _ArtifactReference(
                    context.run or "", row["id"], response.revision, file
                )
                with self._lock:
                    self._artifacts[(reference.run_id, reference.artifact_id)] = reference
                    while len(self._artifacts) > 500:
                        self._artifacts.popitem(last=False)
            rows.append(_object(row))
        return ExtensionPage(
            context,
            tuple(rows),
            response.next_cursor or None,
            response.truncated,
            response.revision,
        )

    def invoke(
        self, action: str, context: TuiContext, payload: Object, *, intent_id: str
    ) -> ActionResult:
        self._select(context)
        if not intent_id or len(intent_id.encode()) > 512:
            raise TuiExtensionError(
                "invalid_input", "A bounded intent ID is required", context=context
            )
        description = self.describe(context)
        descriptor = next((item for item in description.actions if item.id == action), None)
        if descriptor is None:
            raise TuiExtensionError(
                "unsupported", "Action is not supported in this context", context=context
            )
        if not descriptor.available:
            raise TuiExtensionError(
                "read_only" if self._provider._read_only else "conflict",
                descriptor.reason or "Action unavailable",
                context=context,
            )
        digest = hashlib.sha256(
            json.dumps(
                [
                    action,
                    context.scope,
                    context.flow,
                    context.version,
                    context.run,
                    json_plain(payload),
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self._lock:
            previous = self._intents.get(intent_id)
            if previous is not None and previous[0] != digest:
                raise TuiExtensionError("conflict", "Intent payload changed", context=context)
            run_id = "run_" + hashlib.sha256(intent_id.encode()).hexdigest()[:32]
            self._intents[intent_id] = (digest, run_id)
            while len(self._intents) > 1024:
                self._intents.popitem(last=False)
        try:
            if action == "run.start":
                if context.flow is None or context.version is None:
                    raise TuiExtensionError(
                        "invalid_input",
                        "Start requires a pinned workflow version",
                        context=context,
                    )
                self._provider.start_run(
                    context.flow,
                    run_id=run_id,
                    input={key: json_plain(value) for key, value in payload.items()},
                    expected_version=context.version,
                    trigger=TriggerKind.MANUAL,
                )
                value: dict[str, JsonValue] = {"run_id": run_id}
                status: Literal["accepted", "completed"] = "accepted"
            elif action in SCHEDULE_ACTION_IDS:
                request = schedule_request(
                    self._provider.list_workflows(), action, context, payload
                )
                info = self._provider.set_schedule_enabled(
                    request.selector, request.enabled, request.run_input
                )
                value = {"enabled": info.schedule_enabled}
                # The schedule change has finished once the operator answers.
                status = "completed"
            elif action == "run.cancel":
                if payload:
                    raise TuiExtensionError(
                        "invalid_input", "Cancel takes no input", context=context
                    )
                assert context.run is not None
                self._provider.cancel_run(context.run)
                value = {"run_id": context.run, "outcome": "cancellation_requested"}
                status = "accepted"
            else:
                value = self._download(context, payload)
                status = "completed"
        except OperatorCallError as error:
            if (
                action == "run.start"
                and previous is not None
                and error.status == grpc.StatusCode.ALREADY_EXISTS
            ):
                value, status = {"run_id": run_id}, "accepted"
            else:
                raise self._error(error, context) from error
        return ActionResult(context, intent_id, status, None, _object(value))

    def _download(self, context: TuiContext, payload: Object) -> dict[str, JsonValue]:
        artifact_id, destination = payload.get("artifact_id"), payload.get("destination")
        if (
            set(payload) != {"artifact_id", "destination"}
            or not isinstance(artifact_id, str)
            or not isinstance(destination, str)
            or not destination
        ):
            raise TuiExtensionError(
                "invalid_input", "Provide artifact_id and explicit destination", context=context
            )
        with self._lock:
            reference = self._artifacts.get((context.run or "", artifact_id))
        if reference is None:
            raise TuiExtensionError(
                "not_found", "Read the artifact page before downloading", context=context
            )
        path = Path(destination).expanduser().absolute()
        temporary: str | None = None
        chunks = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".ava-download-", dir=path.parent)
            digest, total, eof = hashlib.sha256(), 0, False
            chunks = self._provider._stub.ReadTuiArtifact(
                pb.ReadTuiArtifactRequestV2(
                    run_id=reference.run_id,
                    artifact_id=reference.artifact_id,
                    revision=reference.revision,
                ),
                **self._provider._detail_rpc_kwargs(),
            )
            with os.fdopen(fd, "wb") as output:
                for index, chunk in enumerate(chunks):
                    if (
                        eof
                        or chunk.chunk_index != index
                        or total + len(chunk.data) > reference.size_bytes
                    ):
                        raise TuiExtensionError(
                            "integrity", "Invalid artifact stream", context=context
                        )
                    output.write(chunk.data)
                    digest.update(chunk.data)
                    total += len(chunk.data)
                    eof = chunk.eof
                if (
                    not eof
                    or total != reference.size_bytes
                    or digest.hexdigest() != reference.sha256
                ):
                    raise TuiExtensionError(
                        "integrity", "Artifact digest or size mismatch", context=context
                    )
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, path)  # exclusive publication; never replace an existing path
        except OSError as error:
            raise TuiExtensionError(
                "invalid_input", "Destination is unavailable or already exists", context=context
            ) from error
        except grpc.RpcError as error:
            raise TuiExtensionError(
                "unavailable", "Artifact download unavailable", retryable=True, context=context
            ) from error
        finally:
            if chunks is not None:
                chunks.cancel()
            if temporary is not None:
                os.unlink(temporary)
        return {
            "destination": str(path),
            "sha256": reference.sha256,
            "bytes": reference.size_bytes,
        }

    def subscribe(self, callback: Callable[[Invalidation], None]) -> Callable[[], None]:
        with self._lock:
            if self._closed:
                raise TuiExtensionError("unavailable", "Provider is closed")
            self._callbacks.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)

        return unsubscribe

    def invalidate(self, resource: str, revision: str, *, reset: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            context, callbacks = self._context, tuple(self._callbacks)
            if reset:
                self._artifacts.clear()
        notice = Invalidation(
            context, resource, revision, None, "reset" if reset else "changed", None
        )
        for callback in callbacks:
            callback(notice)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._callbacks.clear()
            self._artifacts.clear()
            self._intents.clear()

    @staticmethod
    def _error(error: OperatorCallError, context: TuiContext) -> TuiExtensionError:
        code = {
            grpc.StatusCode.OUT_OF_RANGE: "cursor_expired",
            grpc.StatusCode.NOT_FOUND: "not_found",
            grpc.StatusCode.PERMISSION_DENIED: "forbidden",
            grpc.StatusCode.UNAUTHENTICATED: "unauthenticated",
            grpc.StatusCode.INVALID_ARGUMENT: "invalid_input",
            grpc.StatusCode.ALREADY_EXISTS: "conflict",
            grpc.StatusCode.FAILED_PRECONDITION: "conflict",
            grpc.StatusCode.UNIMPLEMENTED: "unsupported",
            grpc.StatusCode.RESOURCE_EXHAUSTED: "rate_limited",
            grpc.StatusCode.DATA_LOSS: "integrity",
        }.get(error.status, "unavailable")
        return TuiExtensionError(
            code,
            f"Operator operation failed ({code})",
            retryable=code == "unavailable",
            context=context,
        )
