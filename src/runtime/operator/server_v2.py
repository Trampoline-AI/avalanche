"""gRPC servicer implementing the native V2 operator contract.

The local loopback operator has one implicit authorization scope bound to the
operator instance id. Scope references, lifecycle cursors, and continuations
are revalidated against that scope on every request; staged object-store
attachments are rejected because no remote object plane exists locally.
"""

from __future__ import annotations

import hashlib
import queue
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import grpc
from ulid import ULID

from avalanche.runtime import File

from .convert_v2 import (
    DETAIL_URI_SCHEME,
    RESULT_URI_SCHEME,
    TRACE_URI_SCHEME,
    agent_event_activity_to_v2,
    flow_list_to_v2,
    log_activity_to_v2,
    run_snapshot_to_v2,
    run_summary_to_v2,
    sha256_hex,
    update_envelope_to_v2,
    workflow_info_to_v2,
)
from .operator import (
    InvalidRunIdError,
    Operator,
    RunAlreadyExistsError,
    RunResultNotReadyError,
    RunResultUnavailableError,
    StructuralBaselineUnavailableError,
    _bounded_page_size,
)
from .proto import operator_pb2 as pb
from .proto import operator_pb2_grpc as pb_grpc
from .registry import AmbiguousWorkflow, UnknownWorkflow
from .results import ResultFileAttachment
from .server import TRACE_CHUNK_BYTES, _ambiguous_detail, _decode_json_object

_EVENTS_STREAM = "operator-events"
_MAX_ISSUED_BINDINGS = 10_000
_MAX_ISSUED_CURSORS = 20_000
_CursorIdentity = tuple[str, str, int, str, str]
_ContinuationRegistryKey = tuple[
    str,
    str,
    _CursorIdentity,
    str,
    str,
    str,
    str,
]


@dataclass(frozen=True)
class _ActivityDetailBinding:
    """One local operator-memory body with immutable V2 identity fields."""

    source_kind: str
    run_id: str
    activity_id: str
    run_sequence: int
    object_uri: str
    object_key: str
    size_bytes: int
    body_token: str = ""
    node_id: str = ""
    trace_revision: int = 0


@dataclass(frozen=True)
class _RegisteredActivityDetail:
    binding: _ActivityDetailBinding
    reference: pb.ActivityDetailRefV2


@dataclass(frozen=True)
class _ArtifactBinding:
    """One immutable result attachment owned by the local result store."""

    run_id: str
    artifact_id: str
    run_sequence: int
    object_uri: str
    object_key: str


@dataclass(frozen=True)
class _RegisteredArtifact:
    binding: _ArtifactBinding
    reference: pb.RunOutputArtifactRefV2


@dataclass(frozen=True)
class _ContinuationBinding:
    """The exact request target authorized by one server-issued continuation."""

    reference: pb.ContinuationRefV2
    run_id: str = ""
    node_id: str = ""
    category: str = ""
    workflow_selector: str = ""


class OperatorV2Servicer(pb_grpc.OperatorServiceV2Servicer):
    """V2 gRPC servicer that delegates to an Operator instance."""

    def __init__(self, operator: Operator) -> None:
        self._op = operator
        self._binding_lock = threading.RLock()
        self._issued_cursors: OrderedDict[_CursorIdentity, pb.LifecycleCursorV2] = OrderedDict()
        self._event_ulids_by_sequence: dict[int, str] = {}
        self._sequences_by_event_ulid: dict[str, int] = {}
        self._next_event_sequence = 0
        self._continuations: OrderedDict[_ContinuationRegistryKey, _ContinuationBinding] = (
            OrderedDict()
        )
        self._activity_details: OrderedDict[str, _RegisteredActivityDetail] = OrderedDict()
        self._artifacts: OrderedDict[str, _RegisteredArtifact] = OrderedDict()

    @property
    def _generation(self) -> int:
        generation = int.from_bytes(
            hashlib.sha256(self._op.operator_instance_id.encode("utf-8")).digest()[:8],
            "big",
        )
        return generation or 1

    # ── Scope / cursor helpers ────────────────────────────

    def _scope(self) -> pb.ScopeReferenceV2:
        return pb.ScopeReferenceV2(reference=self._op.operator_instance_id)

    def _cursor(
        self,
        as_of_sequence: int,
        *,
        retained_floor_sequence: int | None = None,
    ) -> pb.LifecycleCursorV2:
        """Issue one complete, locally retained V2 lifecycle cursor."""
        if retained_floor_sequence is None:
            floor_sequence = min(
                self._retained_floor_sequence_for(),
                as_of_sequence,
            )
        else:
            floor_sequence = retained_floor_sequence
        cursor = pb.LifecycleCursorV2(
            stream=_EVENTS_STREAM,
            topology_fingerprint=self._stream_topology_fingerprint(_EVENTS_STREAM),
            stream_generation=self._generation,
            retained_floor_event_ulid=self._event_ulid_for_sequence(floor_sequence),
            event_ulid=self._event_ulid_for_sequence(as_of_sequence),
        )
        self._remember_cursor(cursor)
        return cursor

    def _event_ulid_for_sequence(self, sequence: int) -> str:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("lifecycle sequence must be a non-negative integer")
        with self._binding_lock:
            while self._next_event_sequence <= sequence:
                previous = self._event_ulids_by_sequence.get(self._next_event_sequence - 1)
                candidate = str(ULID())
                if previous is not None and candidate <= previous:
                    candidate = str(ULID.from_int(int(ULID.from_str(previous)) + 1))
                self._event_ulids_by_sequence[self._next_event_sequence] = candidate
                self._sequences_by_event_ulid[candidate] = self._next_event_sequence
                self._next_event_sequence += 1
            return self._event_ulids_by_sequence[sequence]

    def _sequence_for_event_ulid(self, event_ulid: str) -> int | None:
        if not self._is_canonical_event_ulid(event_ulid):
            return None
        with self._binding_lock:
            return self._sequences_by_event_ulid.get(event_ulid)

    @staticmethod
    def _is_canonical_event_ulid(event_ulid: str) -> bool:
        if not event_ulid:
            return False
        try:
            return str(ULID.from_str(event_ulid)) == event_ulid
        except ValueError:
            return False

    def _stream_topology_fingerprint(self, stream: str) -> str:
        material = f"{self._op.operator_instance_id}:{stream}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _retained_floor_sequence_for(self) -> int:
        history_floor, _ = self._op.update_history_bounds()
        return max(0, history_floor)

    def _expected_topology_fingerprint(self, stream: str) -> str:
        return self._stream_topology_fingerprint(stream)

    @staticmethod
    def _cursor_identity(
        cursor: pb.LifecycleCursorV2,
    ) -> _CursorIdentity:
        return (
            cursor.stream,
            cursor.topology_fingerprint,
            cursor.stream_generation,
            cursor.retained_floor_event_ulid,
            cursor.event_ulid,
        )

    @staticmethod
    def _copy_cursor(cursor: pb.LifecycleCursorV2) -> pb.LifecycleCursorV2:
        copied = pb.LifecycleCursorV2()
        copied.CopyFrom(cursor)
        return copied

    @staticmethod
    def _copy_continuation(continuation: pb.ContinuationRefV2) -> pb.ContinuationRefV2:
        copied = pb.ContinuationRefV2()
        copied.CopyFrom(continuation)
        return copied

    @staticmethod
    def _copy_activity_reference(
        reference: pb.ActivityDetailRefV2,
    ) -> pb.ActivityDetailRefV2:
        copied = pb.ActivityDetailRefV2()
        copied.CopyFrom(reference)
        return copied

    @staticmethod
    def _copy_artifact_reference(
        reference: pb.RunOutputArtifactRefV2,
    ) -> pb.RunOutputArtifactRefV2:
        copied = pb.RunOutputArtifactRefV2()
        copied.CopyFrom(reference)
        return copied

    def _remember_cursor(self, cursor: pb.LifecycleCursorV2) -> None:
        key = self._cursor_identity(cursor)
        with self._binding_lock:
            self._issued_cursors[key] = self._copy_cursor(cursor)
            self._issued_cursors.move_to_end(key)
            while len(self._issued_cursors) > _MAX_ISSUED_CURSORS:
                self._issued_cursors.popitem(last=False)

    def _validate_scope(self, scope_ref: pb.ScopeReferenceV2, context) -> None:
        reference = scope_ref.reference
        if reference != self._op.operator_instance_id:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Scope reference is stale; rebaseline against the current operator scope",
            )

    def _cursor_is_complete(self, cursor: pb.LifecycleCursorV2) -> bool:
        return bool(
            cursor.stream
            and cursor.topology_fingerprint
            and cursor.stream_generation
            and self._is_canonical_event_ulid(cursor.retained_floor_event_ulid)
            and self._is_canonical_event_ulid(cursor.event_ulid)
        )

    def _cursor_is_within_retained_window(self, cursor: pb.LifecycleCursorV2) -> bool:
        sequence = self._sequence_for_event_ulid(cursor.event_ulid)
        if sequence is None:
            return False
        if cursor.topology_fingerprint != self._expected_topology_fingerprint(cursor.stream):
            return False
        floor_sequence = self._retained_floor_sequence_for()
        cursor_floor_sequence = self._sequence_for_event_ulid(cursor.retained_floor_event_ulid)
        if cursor_floor_sequence is None or cursor_floor_sequence > floor_sequence:
            return False
        if sequence > self._op.current_sequence:
            return False
        history_floor, _ = self._op.update_history_bounds()
        return sequence >= history_floor - 1

    def _cursor_is_issued_for_stream(
        self,
        cursor: pb.LifecycleCursorV2,
        expected_stream: str,
    ) -> bool:
        if not self._cursor_is_complete(cursor):
            return False
        if cursor.stream != expected_stream or cursor.stream_generation != self._generation:
            return False
        with self._binding_lock:
            issued = self._issued_cursors.get(self._cursor_identity(cursor))
        return issued is not None and self._cursor_is_within_retained_window(cursor)

    def _validate_cursor(
        self,
        cursor: pb.LifecycleCursorV2,
        context,
        *,
        expected_stream: str,
    ) -> None:
        if not self._cursor_is_complete(cursor):
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Cursor must include stream, topology, generation, and retained event ULIDs",
            )
        if cursor.stream != expected_stream:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Cursor belongs to a different stream; rebaseline required",
            )
        if cursor.stream_generation != self._generation:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Cursor belongs to a different stream generation; rebaseline required",
            )
        with self._binding_lock:
            issued = self._issued_cursors.get(self._cursor_identity(cursor))
        if issued is None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Cursor was not issued by this operator view; rebaseline required",
            )
        if not self._cursor_is_within_retained_window(cursor):
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Cursor is outside the retained replay window; rebaseline required",
            )

    @staticmethod
    def _same_continuation(
        left: pb.ContinuationRefV2,
        right: pb.ContinuationRefV2,
    ) -> bool:
        return (
            left.scope_ref.reference == right.scope_ref.reference
            and left.continuation_id == right.continuation_id
            and OperatorV2Servicer._cursor_identity(left.cursor)
            == OperatorV2Servicer._cursor_identity(right.cursor)
        )

    def _continuation_registry_key(
        self,
        continuation: pb.ContinuationRefV2,
        *,
        run_id: str,
        node_id: str,
        category: str,
        workflow_selector: str,
    ) -> _ContinuationRegistryKey:
        return (
            continuation.scope_ref.reference,
            continuation.continuation_id,
            self._cursor_identity(continuation.cursor),
            run_id,
            node_id,
            category,
            workflow_selector,
        )

    def _continuation_token(
        self,
        continuation: pb.ContinuationRefV2,
        context,
        *,
        stream: str,
        run_id: str = "",
        node_id: str = "",
        category: str = "",
        workflow_selector: str = "",
    ) -> str:
        if not continuation.continuation_id:
            return ""
        self._validate_scope(continuation.scope_ref, context)
        self._validate_cursor(continuation.cursor, context, expected_stream=stream)
        key = self._continuation_registry_key(
            continuation,
            run_id=run_id,
            node_id=node_id,
            category=category,
            workflow_selector=workflow_selector,
        )
        with self._binding_lock:
            binding = self._continuations.get(key)
        if binding is None or not self._same_continuation(continuation, binding.reference):
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Continuation was not issued by this operator view; rebaseline required",
            )
        if (
            binding.run_id != run_id
            or binding.node_id != node_id
            or binding.category != category
            or binding.workflow_selector != workflow_selector
        ):
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Continuation does not bind the requested activity target; rebaseline required",
            )
        return continuation.continuation_id

    def _continuation(
        self,
        cursor: pb.LifecycleCursorV2,
        token: str,
        *,
        run_id: str = "",
        node_id: str = "",
        category: str = "",
        workflow_selector: str = "",
    ) -> pb.ContinuationRefV2 | None:
        if not token:
            return None
        continuation = pb.ContinuationRefV2(
            scope_ref=self._scope(),
            continuation_id=token,
            cursor=cursor,
        )
        binding = _ContinuationBinding(
            reference=self._copy_continuation(continuation),
            run_id=run_id,
            node_id=node_id,
            category=category,
            workflow_selector=workflow_selector,
        )
        key = self._continuation_registry_key(
            continuation,
            run_id=run_id,
            node_id=node_id,
            category=category,
            workflow_selector=workflow_selector,
        )
        with self._binding_lock:
            self._continuations[key] = binding
            self._continuations.move_to_end(key)
            while len(self._continuations) > _MAX_ISSUED_BINDINGS:
                self._continuations.popitem(last=False)
        return continuation

    def _activity_continuation(
        self,
        run_id: str,
        node_id: str,
        token: str,
        as_of_sequence: int,
    ) -> pb.ContinuationRefV2 | None:
        return self._continuation(
            self._cursor(as_of_sequence),
            token,
            run_id=run_id,
            node_id=node_id,
            category="agent-events" if node_id else "logs",
        )

    @staticmethod
    def _same_activity_reference(
        left: pb.ActivityDetailRefV2,
        right: pb.ActivityDetailRefV2,
    ) -> bool:
        return (
            left.run_id == right.run_id
            and left.scope_ref.reference == right.scope_ref.reference
            and left.activity_id == right.activity_id
            and left.run_sequence == right.run_sequence
            and left.object_uri == right.object_uri
            and left.object_key == right.object_key
            and left.sha256 == right.sha256
            and left.size_bytes == right.size_bytes
        )

    @staticmethod
    def _same_artifact_reference(
        left: pb.RunOutputArtifactRefV2,
        right: pb.RunOutputArtifactRefV2,
    ) -> bool:
        return (
            left.run_id == right.run_id
            and left.scope_ref.reference == right.scope_ref.reference
            and left.artifact_id == right.artifact_id
            and left.run_sequence == right.run_sequence
            and left.object_uri == right.object_uri
            and left.object_key == right.object_key
            and left.sha256 == right.sha256
            and left.size_bytes == right.size_bytes
        )

    def _read_activity_binding(
        self,
        binding: _ActivityDetailBinding,
        context,
    ) -> bytes:
        try:
            if binding.source_kind == "detail":
                return self._op.read_detail(binding.body_token)
            if binding.source_kind == "trace":
                return self._op.read_trace(
                    binding.run_id,
                    binding.node_id,
                    operator_instance_id=self._op.operator_instance_id,
                    revision=binding.trace_revision,
                ).data
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except KeyError:
            context.abort(grpc.StatusCode.NOT_FOUND, "Detail body not found")
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Unknown activity detail binding")

    def _canonical_activity_reference(
        self,
        binding: _ActivityDetailBinding,
        data: bytes,
    ) -> pb.ActivityDetailRefV2:
        return pb.ActivityDetailRefV2(
            run_id=binding.run_id,
            scope_ref=self._scope(),
            activity_id=binding.activity_id,
            run_sequence=binding.run_sequence,
            object_uri=binding.object_uri,
            object_key=binding.object_key,
            sha256=sha256_hex(data),
            size_bytes=len(data),
        )

    def _register_activity_binding(
        self,
        binding: _ActivityDetailBinding,
        context,
    ) -> pb.ActivityDetailRefV2:
        data = self._read_activity_binding(binding, context)
        if len(data) != binding.size_bytes:
            context.abort(
                grpc.StatusCode.DATA_LOSS,
                "Activity body size does not match its immutable descriptor",
            )
        reference = self._canonical_activity_reference(binding, data)
        entry = _RegisteredActivityDetail(
            binding=binding,
            reference=self._copy_activity_reference(reference),
        )
        with self._binding_lock:
            self._activity_details[reference.object_key] = entry
            self._activity_details.move_to_end(reference.object_key)
            while len(self._activity_details) > _MAX_ISSUED_BINDINGS:
                self._activity_details.popitem(last=False)
        return reference

    def _body_detail_ref(
        self,
        run_id: str,
        activity_id: str,
        body_token: str,
        run_sequence: int,
        size_bytes: int,
        context,
    ) -> pb.ActivityDetailRefV2:
        binding = _ActivityDetailBinding(
            source_kind="detail",
            run_id=run_id,
            activity_id=activity_id,
            run_sequence=run_sequence,
            object_uri=f"{DETAIL_URI_SCHEME}{body_token}",
            object_key=body_token,
            size_bytes=size_bytes,
            body_token=body_token,
        )
        return self._register_activity_binding(binding, context)

    def _trace_detail_ref(
        self,
        run_id: str,
        node_id: str,
        trace_revision: int,
        run_sequence: int,
        size_bytes: int,
        context,
    ) -> pb.ActivityDetailRefV2:
        object_key = f"{run_id}/{node_id}/{trace_revision}"
        binding = _ActivityDetailBinding(
            source_kind="trace",
            run_id=run_id,
            activity_id=f"trace:{node_id}:{trace_revision}",
            run_sequence=run_sequence,
            object_uri=f"{TRACE_URI_SCHEME}{object_key}",
            object_key=object_key,
            size_bytes=size_bytes,
            node_id=node_id,
            trace_revision=trace_revision,
        )
        return self._register_activity_binding(binding, context)

    def _read_registered_activity_detail(
        self,
        detail_ref: pb.ActivityDetailRefV2,
        context,
    ) -> bytes:
        self._validate_scope(detail_ref.scope_ref, context)
        with self._binding_lock:
            entry = self._activity_details.get(detail_ref.object_key)
        if entry is None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Activity detail reference was not issued by this operator view",
            )
        data = self._read_activity_binding(entry.binding, context)
        derived = self._canonical_activity_reference(entry.binding, data)
        if not self._same_activity_reference(entry.reference, derived):
            context.abort(
                grpc.StatusCode.DATA_LOSS,
                "Activity detail body no longer matches its immutable descriptor",
            )
        if not self._same_activity_reference(detail_ref, entry.reference):
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Activity detail reference does not match its immutable binding",
            )
        return data

    def _canonical_artifact_reference(
        self,
        binding: _ArtifactBinding,
        data: bytes,
    ) -> pb.RunOutputArtifactRefV2:
        return pb.RunOutputArtifactRefV2(
            run_id=binding.run_id,
            scope_ref=self._scope(),
            artifact_id=binding.artifact_id,
            run_sequence=binding.run_sequence,
            object_uri=binding.object_uri,
            object_key=binding.object_key,
            sha256=sha256_hex(data),
            size_bytes=len(data),
        )

    def _register_artifact(
        self,
        run_id: str,
        item: ResultFileAttachment,
        run_sequence: int,
    ) -> pb.RunOutputArtifactRefV2:
        object_key = f"{run_id}/{item.attachment_id}"
        binding = _ArtifactBinding(
            run_id=run_id,
            artifact_id=item.attachment_id,
            run_sequence=run_sequence,
            object_uri=f"{RESULT_URI_SCHEME}{run_id}/{item.attachment_id}",
            object_key=object_key,
        )
        reference = self._canonical_artifact_reference(binding, item.content)
        entry = _RegisteredArtifact(
            binding=binding,
            reference=self._copy_artifact_reference(reference),
        )
        with self._binding_lock:
            self._artifacts[reference.object_key] = entry
            self._artifacts.move_to_end(reference.object_key)
            while len(self._artifacts) > _MAX_ISSUED_BINDINGS:
                self._artifacts.popitem(last=False)
        return reference

    def _artifact_bytes(self, binding: _ArtifactBinding, context) -> bytes:
        payload = self._result_payload(binding.run_id, context)
        for item in payload.files:
            if item.attachment_id == binding.artifact_id:
                return item.content
        context.abort(
            grpc.StatusCode.NOT_FOUND,
            f"Artifact {binding.artifact_id} not found for run {binding.run_id}",
        )

    def _read_registered_artifact(
        self,
        artifact_ref: pb.RunOutputArtifactRefV2,
        context,
    ) -> bytes:
        self._validate_scope(artifact_ref.scope_ref, context)
        with self._binding_lock:
            entry = self._artifacts.get(artifact_ref.object_key)
        if entry is None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Artifact reference was not issued by this operator view",
            )
        data = self._artifact_bytes(entry.binding, context)
        derived = self._canonical_artifact_reference(entry.binding, data)
        if not self._same_artifact_reference(entry.reference, derived):
            context.abort(
                grpc.StatusCode.DATA_LOSS,
                "Artifact body no longer matches its immutable descriptor",
            )
        if not self._same_artifact_reference(artifact_ref, entry.reference):
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Artifact reference does not match its immutable binding",
            )
        return data

    def _run_snapshot_message(self, snapshot, context) -> pb.RunSnapshotV2:
        run_id = snapshot.summary.run_id
        as_of_sequence = snapshot.as_of_sequence
        continuations = {
            node.node_id: continuation
            for node in snapshot.nodes
            if (
                continuation := self._activity_continuation(
                    run_id,
                    node.node_id,
                    node.event_page_token,
                    as_of_sequence,
                )
            )
            is not None
        }
        return run_snapshot_to_v2(
            snapshot,
            cursor=self._cursor(as_of_sequence),
            scope_ref=self._scope(),
            trace_detail_ref_for=lambda trace_run_id, node_id, trace, run_sequence: (
                self._trace_detail_ref(
                    trace_run_id,
                    node_id,
                    trace.revision,
                    run_sequence,
                    trace.size_bytes,
                    context,
                )
            ),
            activity_continuations=continuations,
            log_continuation=self._activity_continuation(
                run_id,
                "",
                snapshot.log_page_token,
                as_of_sequence,
            ),
        )

    # ── Discovery ─────────────────────────────────────────

    def DiscoverFlows(self, request, context):  # noqa: N802
        catalog = self._op.get_catalog()
        cursor = self._cursor(catalog.as_of_sequence)
        token = self._continuation_token(
            request.continuation,
            context,
            stream=_EVENTS_STREAM,
            category="flows",
        )
        offset = 0
        if token:
            if not token.startswith("flows:"):
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid flows continuation")
            try:
                offset = int(token[len("flows:") :])
            except ValueError:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid flows continuation")
        flows = sorted(catalog.workflows, key=lambda info: info.selector)
        size = _bounded_page_size(request.page_size)
        page = flows[offset : offset + size]
        next_offset = offset + len(page)
        next_page = (
            self._continuation(cursor, f"flows:{next_offset}", category="flows")
            if next_offset < len(flows)
            else None
        )
        return flow_list_to_v2(
            catalog,
            cursor=cursor,
            flows=[workflow_info_to_v2(info) for info in page],
            next_page=next_page,
            scope_ref=self._scope(),
        )

    # ── Run control ───────────────────────────────────────

    def StartRun(self, request, context):  # noqa: N802
        if not request.run_id:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "run_id is required as the idempotency key",
            )
        try:
            run_input = _decode_input_payload_v2(request)
            run_context = _decode_json_object(request.context_json, "context_json")
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        try:
            run_id = self._op.start_run(
                request.workflow_selector,
                run_id=request.run_id,
                input=run_input,
                context=run_context,
            )
            return pb.StartRunResponseV2(run_id=run_id)
        except InvalidRunIdError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except RunAlreadyExistsError as exc:
            context.abort(grpc.StatusCode.ALREADY_EXISTS, str(exc))
        except AmbiguousWorkflow as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, _ambiguous_detail(exc))
        except UnknownWorkflow as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, exc.args[0])
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))

    def CancelRun(self, request, context):  # noqa: N802
        if not request.run_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "run_id is required")
        self._op.cancel_run(request.run_id)
        return pb.CancelRunResponseV2(run_id=request.run_id)

    # ── Run projections ───────────────────────────────────

    def ListRunSummaries(self, request, context):  # noqa: N802
        token = self._continuation_token(
            request.continuation,
            context,
            stream=_EVENTS_STREAM,
            category="run-summaries",
            workflow_selector=request.workflow_selector,
        )
        try:
            page = self._op.list_run_summaries(
                request.workflow_selector,
                page_size=request.page_size,
                page_token=token,
            )
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except AmbiguousWorkflow as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, _ambiguous_detail(exc))
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        cursor = self._cursor(page.as_of_sequence)
        message = pb.RunSummaryPageV2(
            cursor=cursor,
            runs=[run_summary_to_v2(item) for item in page.runs],
            scope_ref=self._scope(),
        )
        next_page = self._continuation(
            cursor,
            page.next_page_token,
            category="run-summaries",
            workflow_selector=request.workflow_selector,
        )
        if next_page is not None:
            message.next_page.CopyFrom(next_page)
        return message

    def GetRunSnapshot(self, request, context):  # noqa: N802
        try:
            snapshot = self._op.get_latest_run_snapshot(
                request.run_id,
                operator_instance_id=self._op.operator_instance_id,
            )
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        if snapshot is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Run {request.run_id} not found")
        return self._run_snapshot_message(snapshot, context)

    # ── Activity ──────────────────────────────────────────

    def ListRunActivity(self, request, context):  # noqa: N802
        order = int(request.order)
        category = "agent-events" if request.node_id else "logs"
        token = self._continuation_token(
            request.continuation,
            context,
            stream=_EVENTS_STREAM,
            run_id=request.run_id,
            node_id=request.node_id,
            category=category,
        )
        try:
            if request.node_id:
                page = self._op.list_agent_events(
                    request.run_id,
                    request.node_id,
                    page_token=token,
                    page_size=request.page_size,
                    order=order,
                )
                if page.run_id != request.run_id or page.node_id != request.node_id:
                    context.abort(
                        grpc.StatusCode.FAILED_PRECONDITION,
                        "Activity continuation resolved to a different run or node",
                    )
                activities = [
                    agent_event_activity_to_v2(
                        item,
                        run_id=page.run_id,
                        node_id=page.node_id,
                        detail_ref=self._body_detail_ref(
                            page.run_id,
                            f"agent:{page.node_id}:{item.event_sequence}",
                            item.body_token,
                            item.event_sequence,
                            item.size_bytes,
                            context,
                        ),
                    )
                    for item in page.events
                ]
            else:
                page = self._op.list_logs(
                    request.run_id,
                    page_token=token,
                    page_size=request.page_size,
                    order=order,
                )
                activities = [
                    log_activity_to_v2(
                        item,
                        run_id=request.run_id,
                        detail_ref=self._body_detail_ref(
                            request.run_id,
                            f"log:{item.sequence}",
                            item.body_token,
                            item.sequence,
                            item.size_bytes,
                            context,
                        ),
                    )
                    for item in page.logs
                ]
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except KeyError:
            context.abort(grpc.StatusCode.NOT_FOUND, "Activity target not found")
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        cursor = self._cursor(page.as_of_sequence)
        message = pb.RunActivityPageV2(
            cursor=cursor,
            run_id=page.run_id if request.node_id else request.run_id,
            activities=activities,
            scope_ref=self._scope(),
        )
        next_page = self._continuation(
            cursor,
            page.next_page_token,
            run_id=request.run_id,
            node_id=request.node_id,
            category=category,
        )
        if next_page is not None:
            message.next_page.CopyFrom(next_page)
        return message

    def ReadActivityDetail(self, request, context):  # noqa: N802
        data = self._read_registered_activity_detail(request.detail_ref, context)
        yield from _stream_chunks(data, pb.ActivityDetailChunkV2)

    # ── Results ───────────────────────────────────────────

    def _result_payload(self, run_id: str, context):
        try:
            return self._op._get_run_result_payload(run_id)
        except KeyError:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Run {run_id} not found")
        except (RunResultNotReadyError, RunResultUnavailableError) as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            context.abort(grpc.StatusCode.DATA_LOSS, str(exc))

    def _run_sequence(self, run_id: str) -> int:
        snapshot = self._op.get_latest_run_snapshot(
            run_id, operator_instance_id=self._op.operator_instance_id
        )
        return snapshot.summary.created_sequence if snapshot is not None else 0

    @staticmethod
    def _set_file_metadata(descriptor, item) -> None:
        if item.name is not None:
            descriptor.name = item.name
        if item.media_type is not None:
            descriptor.media_type = item.media_type

    def GetRunResult(self, request, context):  # noqa: N802
        payload = self._result_payload(request.run_id, context)
        run_sequence = self._run_sequence(request.run_id)
        value_bytes = payload.value_json.encode("utf-8")
        return pb.RunResultV2(
            cursor=self._cursor(run_sequence),
            run_id=request.run_id,
            scope_ref=self._scope(),
            value=pb.ResultValueV2(
                value_json=payload.value_json,
                sha256=sha256_hex(value_bytes),
                size_bytes=len(value_bytes),
            ),
            files=[
                self._result_file_descriptor(request.run_id, item, run_sequence)
                for item in payload.files
            ],
        )

    def _result_file_descriptor(
        self, run_id: str, item: ResultFileAttachment, run_sequence: int
    ) -> pb.ResultFileDescriptorV2:
        descriptor = pb.ResultFileDescriptorV2(
            artifact_ref=self._register_artifact(run_id, item, run_sequence),
        )
        self._set_file_metadata(descriptor, item)
        return descriptor

    def ListRunOutputArtifacts(self, request, context):  # noqa: N802
        payload = self._result_payload(request.run_id, context)
        run_sequence = self._run_sequence(request.run_id)
        token = self._continuation_token(
            request.continuation,
            context,
            stream=_EVENTS_STREAM,
            run_id=request.run_id,
            category="artifacts",
        )
        offset = 0
        if token:
            if not token.startswith("artifacts:"):
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT, "Invalid artifacts continuation"
                )
            try:
                offset = int(token[len("artifacts:") :])
            except ValueError:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT, "Invalid artifacts continuation"
                )
        size = _bounded_page_size(request.page_size)
        page = payload.files[offset : offset + size]
        next_offset = offset + len(page)
        cursor = self._cursor(run_sequence)
        message = pb.RunOutputArtifactPageV2(
            cursor=cursor,
            run_id=request.run_id,
            scope_ref=self._scope(),
            artifacts=[
                self._artifact_descriptor(request.run_id, item, run_sequence) for item in page
            ],
        )
        if next_offset < len(payload.files):
            message.next_page.CopyFrom(
                self._continuation(
                    cursor,
                    f"artifacts:{next_offset}",
                    run_id=request.run_id,
                    category="artifacts",
                )
            )
        return message

    def _artifact_descriptor(
        self, run_id: str, item: ResultFileAttachment, run_sequence: int
    ) -> pb.RunOutputArtifactDescriptorV2:
        descriptor = pb.RunOutputArtifactDescriptorV2(
            artifact_ref=self._register_artifact(run_id, item, run_sequence),
        )
        self._set_file_metadata(descriptor, item)
        return descriptor

    def ReadRunOutputArtifact(self, request, context):  # noqa: N802
        data = self._read_registered_artifact(request.artifact_ref, context)
        yield from _stream_chunks(data, pb.RunOutputArtifactChunkV2)

    # ── Watch ─────────────────────────────────────────────

    def _watch_reset_envelope(self) -> pb.RunStatusEnvelopeV2:
        history_floor, latest_sequence = self._op.update_history_bounds()
        history_cursor = self._cursor(
            history_floor,
            retained_floor_sequence=max(0, history_floor),
        )
        latest_cursor = self._cursor(
            latest_sequence,
            retained_floor_sequence=max(0, history_floor),
        )
        return pb.RunStatusEnvelopeV2(
            event_ulid=latest_cursor.event_ulid,
            reset_required=pb.ResetRequiredV2(
                history_floor=history_cursor,
                latest_cursor=latest_cursor,
            ),
            cursor=latest_cursor,
            scope_ref=self._scope(),
        )

    def WatchRunStatus(self, request, context):  # noqa: N802
        if request.HasField("scope_ref"):
            self._validate_scope(request.scope_ref, context)
        if not request.HasField("after_cursor"):
            if self._op.current_sequence:
                yield self._watch_reset_envelope()
                return
            after_sequence = 0
        elif not self._cursor_is_issued_for_stream(request.after_cursor, _EVENTS_STREAM):
            yield self._watch_reset_envelope()
            return
        else:
            after_sequence = self._sequence_for_event_ulid(request.after_cursor.event_ulid)
            if after_sequence is None:
                yield self._watch_reset_envelope()
                return
        subscription = self._op.subscribe_operator_updates(
            self._op.operator_instance_id, after_sequence
        )
        try:
            context.send_initial_metadata(())
            while context.is_active():
                try:
                    envelope = subscription.get(timeout=1.0)
                except queue.Empty:
                    continue
                update_sequence = (
                    envelope.update.sequence
                    if envelope.update is not None
                    else envelope.reset_required.latest_sequence
                )
                message = update_envelope_to_v2(
                    envelope,
                    scope_ref=self._scope(),
                    cursor_for=lambda sequence: self._cursor(sequence),
                    activity_continuation_for=(
                        lambda run_id, node_id, token: self._activity_continuation(
                            run_id,
                            node_id,
                            token,
                            update_sequence,
                        )
                    ),
                    body_detail_ref_for=(
                        lambda run_id, activity_id, body_token, run_sequence, size_bytes: (
                            self._body_detail_ref(
                                run_id,
                                activity_id,
                                body_token,
                                run_sequence,
                                size_bytes,
                                context,
                            )
                        )
                    ),
                    trace_detail_ref_for=(
                        lambda run_id, node_id, trace, _sequence: self._trace_detail_ref(
                            run_id,
                            node_id,
                            trace.revision,
                            trace.revision,
                            trace.size_bytes,
                            context,
                        )
                    ),
                )
                yield message
                if envelope.reset_required is not None:
                    return
        finally:
            self._op.unsubscribe_operator_updates(subscription)


def _stream_chunks(data: bytes, chunk_type):
    if not data:
        yield chunk_type(chunk_index=0, data=b"", eof=True)
        return
    for chunk_index, offset in enumerate(range(0, len(data), TRACE_CHUNK_BYTES)):
        chunk = data[offset : offset + TRACE_CHUNK_BYTES]
        yield chunk_type(
            chunk_index=chunk_index,
            data=chunk,
            eof=offset + len(chunk) == len(data),
        )


def _decode_input_payload_v2(request) -> dict[str, Any] | None:
    payload = _decode_json_object(request.input_json, "input_json") or {}
    for attachment in request.input_files:
        if not attachment.field_name:
            raise ValueError("input file attachment is missing field_name")
        if attachment.object_uri or attachment.object_key:
            raise ValueError(
                "staged object attachments are not supported by the local loopback operator"
            )
        if attachment.field_name in payload:
            raise ValueError(f"Duplicate input field '{attachment.field_name}'")
        payload[attachment.field_name] = File(
            name=attachment.name or None,
            content=bytes(attachment.inline_bytes),
            content_type=attachment.media_type or None,
            sha256=attachment.sha256 or None,
        )
    return payload or None
